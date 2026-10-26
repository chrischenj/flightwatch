"""Kiwi.com 公开 GraphQL 接口数据源。

为什么用它:
  * 不需要 API key，能直接拿到「单程 + 指定舱位」的实时最低价；
  * 返回结果粒度到每个航段（航司、航班号、起降机场与当地时间、舱位），
    正好可以用来拼「分段买票 / 某一段升公务舱」的方案；
  * 支持自助中转（self-transfer），这对分段购票的可行性判断很重要。

注意:
  * 这是 Kiwi 前端使用的接口，不是官方承诺的开放 API。请低频使用（本模块
    默认 0.8 秒一次请求 + 30 分钟磁盘缓存），仅作个人比价参考，下单前必须以
    航司/OTA 页面为准。
  * 只返回「最便宜的若干条」，不是全量运价；结果为枚举价，非报价承诺。
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .. import airports
from ..models import Leg, RoundTrip, Segment
from .base import CACHE_DIR, DiskCache, RateLimiter

ENDPOINT = "https://api.skypicker.com/umbrella/v2/graphql"
PARTNER = "skypicker"
PARTNER_MARKET = "cn"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.kiwi.com",
    "Referer": "https://www.kiwi.com/",
}

_SEARCH_QUERY = """
query SearchOnewayItinerariesQuery(
  $search: SearchOnewayInput, $filter: ItinerariesFilterInput, $options: ItinerariesOptionsInput
) {
  onewayItineraries(search: $search, filter: $filter, options: $options) {
    __typename
    ... on AppError { message }
    ... on Itineraries {
      metadata { itinerariesCount }
      itineraries {
        __typename
        ... on ItineraryOneWay {
          id
          price { amount }
          duration
          cabinClasses
          bagsInfo { includedCheckedBags includedHandBags }
          sector {
            sectorSegments {
              guarantee
              layover { duration isBaggageRecheck isStationChange }
              segment {
                code
                cabinClass
                duration
                carrier { code name }
                source { localTime station { code } }
                destination { localTime station { code } }
              }
            }
          }
        }
      }
    }
  }
}
"""

_RETURN_QUERY = """
query SearchReturnItinerariesQuery(
  $search: SearchReturnInput, $filter: ItinerariesFilterInput, $options: ItinerariesOptionsInput
) {
  returnItineraries(search: $search, filter: $filter, options: $options) {
    __typename
    ... on AppError { message }
    ... on Itineraries {
      itineraries {
        __typename
        ... on ItineraryReturn {
          id
          price { amount }
          duration
          cabinClasses
          bagsInfo { includedCheckedBags includedHandBags }
          outbound {
            duration
            sectorSegments {
              layover { duration isBaggageRecheck }
              segment {
                code cabinClass duration
                carrier { code }
                source { localTime station { code } }
                destination { localTime station { code } }
              }
            }
          }
          inbound {
            duration
            sectorSegments {
              layover { duration isBaggageRecheck }
              segment {
                code cabinClass duration
                carrier { code }
                source { localTime station { code } }
                destination { localTime station { code } }
              }
            }
          }
        }
      }
    }
  }
}
"""

_PLACES_QUERY = """
query($s: PlacesSearchInput) {
  places(search: $s, first: 8) {
    __typename
    ... on AppError { message }
    ... on PlaceConnection {
      edges { node {
        __typename id name
        gps { lat lng }
        ... on Station { code }
      } }
    }
  }
}
"""

# 城市码 -> Kiwi 城市 id 的持久缓存（这个几乎不变，单独放一个文件）
_PLACE_CACHE = os.path.join(CACHE_DIR, "places.json")


class KiwiError(RuntimeError):
    pass


class KiwiProvider:
    name = "Kiwi.com 公开接口"
    live = True

    def __init__(
        self,
        currency: str = "CNY",
        adults: int = 1,
        max_stops: int = 1,
        max_duration_min: int = 1500,
        limit: int = 6,
        min_interval: float = 0.8,
        timeout: float = 45.0,
        cache_ttl: float = 1800.0,
    ):
        self.currency = currency.upper()
        self.adults = max(1, int(adults))
        self.max_stops = max(0, int(max_stops))
        self.max_duration_min = int(max_duration_min)
        self.limit = limit
        self.timeout = timeout
        self._limiter = RateLimiter(min_interval)
        self._cache = DiskCache("kiwi", ttl_seconds=cache_ttl)
        self._places: dict[str, str] = {}
        self.last_errors: list[str] = []
        self._load_places()

    # ------------------------------------------------------------------ 基础设施
    def _load_places(self) -> None:
        try:
            with open(_PLACE_CACHE, encoding="utf-8") as f:
                self._places = json.load(f)
        except (OSError, ValueError):
            self._places = {}

    def _save_places(self) -> None:
        try:
            os.makedirs(os.path.dirname(_PLACE_CACHE), exist_ok=True)
            with open(_PLACE_CACHE, "w", encoding="utf-8") as f:
                json.dump(self._places, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def _post(self, query: str, variables: dict, retries: int = 3) -> dict:
        """POST 到 GraphQL，带限速、重试、磁盘缓存。"""
        cache_key = json.dumps({"q": query, "v": variables}, sort_keys=True, ensure_ascii=False)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        last_err: Optional[Exception] = None
        for attempt in range(retries):
            self._limiter.wait()
            req = urllib.request.Request(ENDPOINT, data=body, headers=_HEADERS, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read())
                if data.get("errors"):
                    # GraphQL 校验错误重试没有意义，直接抛出
                    raise KiwiError(str(data["errors"])[:400])
                self._cache.set(cache_key, data)
                return data
            except urllib.error.HTTPError as e:
                detail = e.read()[:300].decode("utf-8", "replace")
                last_err = KiwiError(f"HTTP {e.code}: {detail}")
                if e.code == 400:
                    break
            except Exception as e:  # noqa: BLE001  网络抖动一律重试
                last_err = e
            time.sleep(1.0 * (attempt + 1) + random.uniform(0, 0.6))
        self.last_errors.append(str(last_err)[:200] if last_err else "unknown error")
        raise KiwiError(str(last_err))

    # ------------------------------------------------------------------ 往返
    def return_trips(
        self, origin: str, dest: str, out_date: str, ret_date: str, cabin: str,
        max_stops: Optional[int] = None,
    ) -> list[RoundTrip]:
        """查询一张真正的往返票（去程+回程绑在一起），按价格升序。

        max_stops=0 表示去回程都要直飞。
        """
        try:
            origin_id = self.resolve_city(origin)
            dest_id = self.resolve_city(dest)
        except KiwiError as e:
            self.last_errors.append(str(e)[:200])
            return []
        if origin_id == dest_id or not ret_date:
            return []
        stops = self.max_stops if max_stops is None else max_stops

        variables = {
            "search": {
                "itinerary": {
                    "source": {"ids": [origin_id]},
                    "destination": {"ids": [dest_id]},
                    "outboundDepartureDate": {
                        "start": f"{out_date}T00:00:00", "end": f"{out_date}T23:59:59",
                    },
                    "inboundDepartureDate": {
                        "start": f"{ret_date}T00:00:00", "end": f"{ret_date}T23:59:59",
                    },
                },
                "passengers": {"adults": self.adults},
                "cabinClass": {
                    "cabinClass": cabin,
                    "applyMixedClasses": cabin in ("ECONOMY", "PREMIUM_ECONOMY"),
                },
            },
            "filter": {"limit": self.limit, "maxStopsCount": stops,
                       "enableSelfTransfer": True, "allowOvernightStopover": True},
            "options": {"currency": self.currency.lower(), "locale": "en",
                        "partner": PARTNER, "partnerMarket": PARTNER_MARKET,
                        "sortBy": "PRICE", "sortOrder": "ASCENDING"},
        }
        try:
            data = self._post(_RETURN_QUERY, variables)
        except KiwiError:
            return []

        node = ((data.get("data") or {}).get("returnItineraries")) or {}
        if node.get("__typename") == "AppError":
            self.last_errors.append(f"往返 {origin}->{dest}: {node.get('message')}")
            return []
        if node.get("__typename") != "Itineraries":
            return []

        out: list[RoundTrip] = []
        # 往返票的单程时长上限放宽到至少 30 小时：长途一次中转本身就要 20-30h，
        # 用单程票的上限会把真实可用的往返票全部误杀。
        cap = max(self.max_duration_min, 1800)
        for it in node.get("itineraries") or []:
            try:
                price = float(it["price"]["amount"])
            except (KeyError, TypeError, ValueError):
                continue
            ob = _sector_to_leg(it.get("outbound") or {}, origin, dest, cabin)
            ib = _sector_to_leg(it.get("inbound") or {}, dest, origin, cabin)
            if ob is None or ib is None:
                continue
            if ob.duration_min > cap or ib.duration_min > cap:
                continue
            out.append(
                RoundTrip(
                    origin=origin, destination=dest,
                    outbound=ob, inbound=ib,
                    price=price, currency=self.currency, cabin=cabin,
                    provider="kiwi",
                )
            )
        out.sort(key=lambda r: (r.price, r.duration_min))
        return out

    # ------------------------------------------------------------------ 城市解析
    def resolve_place(self, term: str) -> Optional[dict]:
        """把任意城市/机场文本（中文、英文、三字码）实时交给 Kiwi 解析。

        返回 {"kiwi_id","code","name","lat","lon","kind"} 或 None。
        这是「不再依赖本地城市表」的关键：本地表只是缓存和离线兜底。
        """
        term = (term or "").strip()
        if not term:
            return None
        key = "term:" + term.lower()
        cached = self._places.get(key)
        if isinstance(cached, dict):
            want = term.strip().upper()
            # 三字码查询必须精确匹配，否则丢弃旧缓存重查（防止 "MXP" 缓存成 "Maputo"）
            if not (len(want) == 3 and want.isalpha()) or str(cached.get("code") or "").upper() == want:
                return cached
        try:
            data = self._post(_PLACES_QUERY, {"s": {"term": term, "slugs": []}})
        except KiwiError:
            return None
        node = ((data.get("data") or {}).get("places")) or {}
        if node.get("__typename") != "PlaceConnection":
            return None
        edges = node.get("edges") or []
        if not edges:
            return None

        best = None
        want = term.upper()
        is_iata = len(want) == 3 and want.isalpha()
        # 1) 三字码必须精确命中机场码，否则不认（防止 "MXP" 被模糊匹配成 "Maputo"）
        if is_iata:
            for edge in edges:
                n = edge["node"]
                if n.get("__typename") == "Station" and str(n.get("code") or "").upper() == want:
                    best = n
                    break
            if best is None:
                return None
        # 2) 名称精确匹配
        if best is None:
            for edge in edges:
                n = edge["node"]
                if str(n.get("name") or "").lower() == term.lower():
                    best = n
                    break
        # 3) 城市优先，其次机场
        if best is None:
            for edge in edges:
                if edge["node"].get("__typename") == "City":
                    best = edge["node"]
                    break
        if best is None:
            for edge in edges:
                if edge["node"].get("__typename") == "Station":
                    best = edge["node"]
                    break
        if best is None:
            best = edges[0]["node"]

        gps = best.get("gps") or {}
        info = {
            "kiwi_id": best.get("id"),
            "code": best.get("code") or _code_from_id(best.get("id")),
            "name": best.get("name") or term,
            "lat": gps.get("lat"),
            "lon": gps.get("lng"),
            "kind": best.get("__typename"),
        }
        self._places[key] = info
        self._save_places()
        return info

    def resolve_city(self, code: str) -> str:
        """城市码 -> Kiwi 地点 id（如 "City:beijing_cn"）。"""
        code = airports.resolve_city(code) or code.upper()
        meta = airports.city(code) or {}
        if meta.get("kiwi_id"):
            return meta["kiwi_id"]
        cached = self._places.get(code)
        if isinstance(cached, str):
            return cached

        term = meta.get("en") or code
        data = self._post(_PLACES_QUERY, {"s": {"term": term, "slugs": []}})
        node = ((data.get("data") or {}).get("places")) or {}
        if node.get("__typename") != "PlaceConnection" or not node.get("edges"):
            raise KiwiError(f"无法解析城市 {code}（{term}）")
        best = None
        for edge in node["edges"]:
            n = edge["node"]
            if n["__typename"] == "City":
                best = n["id"]
                break
        if best is None:
            best = node["edges"][0]["node"]["id"]
        self._places[code] = best
        self._save_places()
        return best

    # ------------------------------------------------------------------ 查询
    def _variables(self, origin_id: str, dest_id: str, date: str, cabin: str,
                   max_stops: Optional[int] = None) -> dict:
        mixed = cabin in ("ECONOMY", "PREMIUM_ECONOMY")
        stops = self.max_stops if max_stops is None else max_stops
        return {
            "search": {
                "itinerary": {
                    "source": {"ids": [origin_id]},
                    "destination": {"ids": [dest_id]},
                    "outboundDepartureDate": {
                        "start": f"{date}T00:00:00",
                        "end": f"{date}T23:59:59",
                    },
                },
                "passengers": {"adults": self.adults},
                "cabinClass": {"cabinClass": cabin, "applyMixedClasses": mixed},
            },
            "filter": {"limit": self.limit, "maxStopsCount": stops,
                       "enableSelfTransfer": True, "allowOvernightStopover": True},
            "options": {"currency": self.currency.lower(), "locale": "en",
                        "partner": PARTNER, "partnerMarket": PARTNER_MARKET,
                        "sortBy": "PRICE", "sortOrder": "ASCENDING"},
        }

    def legs(self, origin: str, dest: str, date: str, cabin: str,
             max_stops: Optional[int] = None) -> list[Leg]:
        """查询某天某舱位的候选票段，按价格升序。失败返回空列表并记录错误。

        max_stops=0 表示只要直飞（整段不中转）。
        """
        try:
            origin_id = self.resolve_city(origin)
            dest_id = self.resolve_city(dest)
        except KiwiError as e:
            self.last_errors.append(str(e)[:200])
            return []
        if origin_id == dest_id:
            return []
        try:
            data = self._post(_SEARCH_QUERY,
                              self._variables(origin_id, dest_id, date, cabin, max_stops))
        except KiwiError:
            return []

        node = ((data.get("data") or {}).get("onewayItineraries")) or {}
        if node.get("__typename") == "AppError":
            self.last_errors.append(f"{origin}->{dest} {date}: {node.get('message')}")
            return []
        if node.get("__typename") != "Itineraries":
            return []

        out: list[Leg] = []
        for it in node.get("itineraries") or []:
            leg = self._to_leg(it, origin, dest, cabin, date)
            if leg is None:
                continue
            if leg.duration_min > self.max_duration_min:
                continue
            out.append(leg)
        out.sort(key=lambda l: (l.price, l.duration_min))
        return out

    # ------------------------------------------------------------------ 解析结果
    def _to_leg(self, it: dict, origin: str, dest: str, cabin: str, date: str) -> Optional[Leg]:
        try:
            amount = float(it["price"]["amount"])
        except (KeyError, TypeError, ValueError):
            return None

        segments: list[Segment] = []
        self_transfer = False
        layover_total = 0.0
        for ss in (it.get("sector") or {}).get("sectorSegments") or []:
            seg = ss.get("segment") or {}
            src = seg.get("source") or {}
            dst = seg.get("destination") or {}
            lay = ss.get("layover") or {}
            if lay.get("isBaggageRecheck"):
                self_transfer = True
            gap = lay.get("duration")
            if gap:
                layover_total += float(gap)
            segments.append(
                Segment(
                    carrier=str((seg.get("carrier") or {}).get("code") or ""),
                    flight_no=str(seg.get("code") or ""),
                    origin=str((src.get("station") or {}).get("code") or ""),
                    destination=str((dst.get("station") or {}).get("code") or ""),
                    depart_local=str(src.get("localTime") or ""),
                    arrive_local=str(dst.get("localTime") or ""),
                    duration_min=int(int(seg.get("duration") or 0) / 60),  # Kiwi 该字段单位是秒
                    cabin=str(seg.get("cabinClass") or cabin),
                )
            )
        if not segments:
            return None
        if not segments[0].depart_local or not segments[-1].arrive_local:
            return None

        cabins = {s.cabin for s in segments} or {cabin}
        main_cabin = cabin if cabin in cabins else sorted(cabins)[0]
        # 注意：Kiwi 的 duration 单位是「秒」。绝不能拿起降「当地时间」相减，
        # 那会把时区差算进去（东行偏小、西行偏大，甚至为负）。
        raw_duration = it.get("duration")
        if raw_duration:
            total = float(raw_duration) / 60.0
        else:
            total = sum(s.duration_min for s in segments) + layover_total / 60.0

        bags = it.get("bagsInfo") or {}
        checked = bags.get("includedCheckedBags")
        note = "含托运行李" if checked else "不含托运行李"
        return Leg(
            origin=origin,
            destination=dest,
            depart_local=segments[0].depart_local,
            arrive_local=segments[-1].arrive_local,
            duration_min=int(total),
            price=amount,
            currency=self.currency,
            cabin=main_cabin,
            segments=segments,
            provider="kiwi",
            booking_url=_google_flights_link(
                segments[0].origin, segments[-1].destination, segments[0].depart_local
            ),
            stops=max(0, len(segments) - 1),
            self_transfer=self_transfer,
            baggage_note=note,
        )


def _sector_to_leg(sector: dict, origin: str, dest: str, cabin: str) -> Optional[Leg]:
    """把往返票的 outbound / inbound sector 转成一个 Leg（价格未知，置 0）。"""
    segments: list[Segment] = []
    for ss in sector.get("sectorSegments") or []:
        seg = ss.get("segment") or {}
        src = (seg.get("source") or {}).get("localTime")
        dst = (seg.get("destination") or {}).get("localTime")
        if not src or not dst:
            continue
        segments.append(
            Segment(
                carrier=str((seg.get("carrier") or {}).get("code") or ""),
                flight_no=str(seg.get("code") or ""),
                origin=str(((seg.get("source") or {}).get("station") or {}).get("code") or ""),
                destination=str(((seg.get("destination") or {}).get("station") or {}).get("code") or ""),
                depart_local=str(src),
                arrive_local=str(dst),
                duration_min=int(int(seg.get("duration") or 0) / 60),
                cabin=str(seg.get("cabinClass") or cabin),
            )
        )
    if not segments:
        return None
    raw = sector.get("duration")
    duration_min = int(float(raw) / 60.0) if raw else sum(s.duration_min for s in segments)
    return Leg(
        origin=origin, destination=dest,
        depart_local=segments[0].depart_local,
        arrive_local=segments[-1].arrive_local,
        duration_min=duration_min,
        price=0.0,               # 往返票价格不可按方向拆分，总价见 RoundTrip.price
        currency="",
        cabin=cabin,
        segments=segments,
        provider="kiwi",
        stops=max(0, len(segments) - 1),
    )


def _code_from_id(place_id: str) -> str:
    """把 Kiwi 地点 id 变成一个短代码作兜底展示（City:ljubljana_si -> LJU）。"""
    slug = str(place_id or "").split(":", 1)[-1]
    head = slug.split("_")[0].replace("-", "")
    code = "".join(ch for ch in head.upper() if ch.isalnum())[:3]
    return code or "UNK"


def _google_flights_link(from_code: str, to_code: str, depart_local: str) -> str:
    """生成一个人工核价用的跳转链接（Google Flights 深链，仅作参考入口）。"""
    day = (depart_local or "")[:10]
    q = f"Flights from {from_code} to {to_code} on {day}" if day else f"Flights from {from_code} to {to_code}"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)

"""离线假数据源：不联网也能跑通「拆票 + 升舱建议」全流程。

用途:
  * 演示、开发、单元测试；
  * 网络不通时的降级方案（界面会标注「示例数据」）。

价格用「距离线性模型 + 舱位倍率 + 确定性抖动」合成，量级参照真实长航线单程票价，
只保证相对关系合理，不代表任何真实报价。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from .. import airports
from ..models import Leg, RoundTrip, Segment

_SPLIT_HUBS = ["IST", "DOH", "DXB", "SIN", "AMS", "FRA", "ICN", "HEL", "AUH", "CAI"]
_CARRIERS = ["CA", "MU", "CZ", "HU", "3U", "TK", "QR", "EK", "SQ", "LH", "KL", "AY", "EY", "MS"]


def _seed(*parts: str) -> int:
    raw = "|".join(parts).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:8], 16)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class FixtureProvider:
    name = "离线示例数据"
    live = False

    def __init__(self, currency: str = "CNY", max_stops: int = 1, **_kw):
        self.currency = currency
        self.max_stops = max_stops

    def legs(self, origin: str, dest: str, date: str, cabin: str,
             max_stops=None) -> list[Leg]:
        o = airports.resolve_city(origin) or origin
        d = airports.resolve_city(dest) or dest
        if o == d:
            return []
        stops_cap = self.max_stops if max_stops is None else int(max_stops)
        km = airports.distance_km(o, d)
        if km <= 0:
            km = 3000.0
        shift = airports.tz_shift_h(o, d)
        rng = _seed(o, d, date)

        options = []
        # 三种典型产品：直飞（贵一点）、一次中转（便宜）、红眼/绕行（最便宜）
        plans = [
            (0, 1.00, 0.0),
            (1, 0.84, 0.0),
            (1, 0.78, 0.0),
        ]
        if km > 9000 or stops_cap < 1:
            plans = [(0, 1.0, 0.0)]
        for i, (stops, factor, _) in enumerate(plans):
            if stops > stops_cap:
                continue
            hour = [23, 2, 9, 14, 18, 6][(rng + i * 3) % 6]
            dep = datetime.strptime(date, "%Y-%m-%d").replace(hour=hour, minute=[0, 25, 40, 55][(rng + i) % 4])
            fly_min = int(km / 760.0 * 60) + 45 + stops * (110 + (rng + i) % 70)
            # 实际经过时间（不含时区影响），arr 只是给界面看的当地墙钟时间
            elapsed_min = fly_min
            arr = dep + timedelta(minutes=fly_min + int(shift * 60))
            econ = 380.0 + 0.30 * km
            price = econ * factor
            if cabin == "BUSINESS":
                price = (1400.0 + 1.45 * km) * (factor + 0.06 * i)
            elif cabin == "PREMIUM_ECONOMY":
                price = econ * 2.05 * factor
            elif cabin == "FIRST_CLASS":
                price = (2600.0 + 2.3 * km) * factor
            price = round(price / 10.0) * 10.0

            carrier = _CARRIERS[(rng + i) % len(_CARRIERS)]
            segments: list[Segment] = []
            if stops == 0:
                segments.append(
                    Segment(
                        carrier=carrier,
                        flight_no=str(100 + (rng + i) % 800),
                        origin=_airport_of(o),
                        destination=_airport_of(d),
                        depart_local=_fmt(dep),
                        arrive_local=_fmt(arr),
                        duration_min=fly_min,
                        cabin=cabin,
                    )
                )
            else:
                hub = _SPLIT_HUBS[(rng + i) % len(_SPLIT_HUBS)]
                if hub in (o, d):
                    hub = _SPLIT_HUBS[(rng + i + 1) % len(_SPLIT_HUBS)]
                first = int(fly_min * 0.45)
                mid = dep + timedelta(minutes=first)
                mid2 = mid + timedelta(minutes=90 + (rng % 60))
                segments.append(
                    Segment(
                        carrier=carrier,
                        flight_no=str(100 + (rng + i) % 800),
                        origin=_airport_of(o),
                        destination=hub,
                        depart_local=_fmt(dep),
                        arrive_local=_fmt(mid),
                        duration_min=first,
                        cabin=cabin,
                    )
                )
                segments.append(
                    Segment(
                        carrier=carrier,
                        flight_no=str(900 + (rng + i * 7) % 90),
                        origin=hub,
                        destination=_airport_of(d),
                        depart_local=_fmt(mid2),
                        arrive_local=_fmt(arr),
                        duration_min=fly_min - first - 90,
                        cabin=cabin,
                    )
                )
            options.append(
                Leg(
                    origin=o,
                    destination=d,
                    depart_local=_fmt(dep),
                    arrive_local=_fmt(arr),
                    duration_min=int(elapsed_min),
                    price=price,
                    currency=self.currency,
                    cabin=cabin,
                    segments=segments,
                    provider="fixture",
                    stops=stops,
                    self_transfer=stops > 0,
                    baggage_note="含托运行李" if cabin != "ECONOMY" or (rng % 3) else "不含托运行李",
                )
            )
        options.sort(key=lambda l: (l.price, l.duration_min))
        return options

    # ------------------------------------------------------------------ 往返
    def return_trips(self, origin: str, dest: str, out_date: str, ret_date: str, cabin: str,
                     max_stops=None):
        """离线合成往返票：去程 + 回程各取一条，价格给一点「买往返更便宜」的折扣。"""
        outs = self.legs(origin, dest, out_date, cabin, max_stops=max_stops)
        ins = self.legs(dest, origin, ret_date, cabin, max_stops=max_stops)
        if not outs or not ins or not ret_date:
            return []
        out_leg, in_leg = outs[0], ins[0]
        total = round((out_leg.price + in_leg.price) * 0.88 / 10.0) * 10.0
        return [
            RoundTrip(
                origin=airports.resolve_city(origin) or origin,
                destination=airports.resolve_city(dest) or dest,
                outbound=out_leg,
                inbound=in_leg,
                price=total,
                currency=self.currency,
                cabin=cabin,
                provider="fixture",
            )
        ]


def _airport_of(code: str) -> str:
    """城市码 -> 该城市的主机场三字码（没有映射时退回城市码）。"""
    return {
        "BJS": "PEK", "SHA": "PVG", "CAN": "CAN", "SZX": "SZX", "CTU": "TFU",
        "CKG": "CKG", "HGH": "HGH", "NKG": "NKG", "WUH": "WUH", "XIY": "XIY",
        "KMG": "KMG", "XMN": "XMN", "TAO": "TAO", "TSN": "TSN", "CSX": "CSX",
        "CGO": "CGO", "HRB": "HRB", "SHE": "SHE", "URC": "URC", "HKG": "HKG",
        "TPE": "TPE", "JFK": "JFK", "LAX": "LAX", "SFO": "SFO", "ORD": "ORD",
        "BOS": "BOS", "IAD": "IAD", "DFW": "DFW", "SEA": "SEA", "YVR": "YVR",
        "YYZ": "YYZ", "LHR": "LHR", "CDG": "CDG", "FRA": "FRA", "MUC": "MUC",
        "AMS": "AMS", "FCO": "FCO", "MAD": "MAD", "ZRH": "ZRH", "VIE": "VIE",
        "HEL": "HEL", "CPH": "CPH", "LIS": "LIS", "SVO": "SVO", "IST": "IST",
        "DOH": "DOH", "DXB": "DXB", "AUH": "AUH", "RUH": "RUH", "TLV": "TLV",
        "DEL": "DEL", "BOM": "BOM", "SIN": "SIN", "KUL": "KUL", "BKK": "BKK",
        "CGK": "CGK", "MNL": "MNL", "ICN": "ICN", "NRT": "NRT", "OSA": "KIX",
        "TAS": "TAS", "ALA": "ALA", "CAI": "CAI", "ADD": "ADD", "NBO": "NBO",
        "JNB": "JNB", "SYD": "SYD", "MEL": "MEL", "AKL": "AKL", "MEX": "MEX",
        "GRU": "GRU", "EZE": "EZE", "LIM": "LIM", "SCL": "SCL", "HAK": "HAK",
        "SYX": "SYX", "FOC": "FOC", "NNG": "NNG", "MFM": "MFM",
        # 扩充
        "MIL": "MXP", "VCE": "VCE", "NAP": "NAP", "BCN": "BCN", "BER": "BER",
        "PRG": "PRG", "BUD": "BUD", "ATH": "ATH", "DUB": "DUB", "STO": "ARN",
        "OSL": "OSL", "WAW": "WAW", "GVA": "GVA", "BRU": "BRU", "MAN": "MAN",
        "EDI": "EDI", "NCE": "NCE", "HAM": "HAM", "DUS": "DUS", "MIA": "MIA",
        "LAS": "LAS", "HOU": "IAH", "ATL": "ATL", "DEN": "DEN", "HNL": "HNL",
        "HAN": "HAN", "SGN": "SGN", "DPS": "DPS", "HKT": "HKT", "NGO": "NGO",
        "FUK": "FUK", "CTS": "CTS", "CMB": "CMB", "KTM": "KTM", "JED": "JED",
        "MCT": "MCT", "KWI": "KWI", "BAK": "GYD", "TBS": "TBS", "CAS": "CMN",
        "RAK": "RAK", "CPT": "CPT", "MRU": "MRU", "BNE": "BNE", "PER": "PER",
    }.get(code, code)

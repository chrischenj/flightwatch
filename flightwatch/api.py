"""集成层：把一次「分舱段查询」变成可直接渲染的 JSON（Web / CLI 共用）。

调用顺序:
  Query -> 候选枢纽 -> 预取报价（并发，先把缓存喂热）-> engine 组装方案
        -> engine 评分/组合/推荐 -> 统一 JSON
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date, timedelta
from typing import Callable, Optional

from . import airports
from .engine import optimize, score, split
from .models import CABIN_CN, Query
from .providers.fixture import FixtureProvider
from .providers.kiwi import KiwiProvider

CABINS_TO_PRICE = ("ECONOMY", "BUSINESS")
DEFAULT_MAX_HUBS = 4
MAX_DATE_WINDOW = 2

WARNINGS = [
    "分段买票不是联程票：前一段延误/取消，后一段不负责改签，误机一般按自愿弃程处理。",
    "行李通常不能直挂，跨航司中转需取出行李重新托运、重新安检，务必留足中转时间。",
    "请核对中转国签证/过境许可要求（美国、申根、英国、加拿大尤其严格）。",
    "价格为公开接口实时枚举的最低档位，可能含缓存延迟，下单前必须以航司或 OTA 页面复核。",
]

# 价格口径（已在 Kiwi 结算页核实：结算页价格框写的是
# "Includes all taxes, fees, surcharges, and Kiwi.com service fees"，
# 且搜索结果 price.amount 与最低可订方案 bookingOptions[0].price 完全一致）
PRICE_NOTE = (
    "价格口径：每段显示的是该段「最低可订方案」的总价，已包含航司票价、各项税费与附加费、"
    "以及平台服务费；不含未标注为「含托运行李」的行李费、选座、餐食等可选服务。"
)


def _resolve_remote(term: str, currency: str = "CNY") -> Optional[str]:
    """本地表里没有的城市，实时交给 Kiwi 的 places 接口解析。

    这样就不再受「我硬编码的城市表」限制：Kiwi 认识的地方都能查。
    解析结果登记进 airports 内存表（进程内有效），Identifiers 级别缓存到 data/cache。
    """
    provider = KiwiProvider(currency=currency, min_interval=0.0, cache_ttl=86400)
    try:
        info = provider.resolve_place(term)
    except Exception:  # noqa: BLE001  网络问题就当解析失败，走下面的报错
        return None
    if not info or not info.get("kiwi_id"):
        return None
    lat = info.get("lat")
    lon = info.get("lon")
    try:
        tz = round(float(lon) / 15.0) if lon is not None else 0.0
    except (TypeError, ValueError):
        tz = 0.0
    return airports.register_city(
        code=info.get("code") or "UNK",
        zh=term,
        en=info.get("name") or term,
        lat=lat if isinstance(lat, (int, float)) else 0.0,
        lon=lon if isinstance(lon, (int, float)) else 0.0,
        tz=tz,
        kiwi_id=info["kiwi_id"],
    )


def make_query(
    origin: str,
    destination: str,
    date: str,
    date_window: int = 0,
    adults: int = 1,
    currency: str = "CNY",
    hubs: Optional[list[str]] = None,
    max_stops: int = 1,
    max_ground_min: int = 1800,
    allow_remote: bool = True,
    return_date: str = "",
) -> Query:
    """参数校验 + 城市码归一化。

    本地城市表命中就直接用（快）；没命中且允许联网时，实时问 Kiwi 的 places 接口，
    所以城市名不再受本地表限制。
    """
    o = airports.resolve_city(origin)
    if not o and allow_remote:
        o = _resolve_remote(origin, currency)
    d = airports.resolve_city(destination)
    if not d and allow_remote:
        d = _resolve_remote(destination, currency)
    if not o:
        raise ValueError(
            f"无法识别出发城市「{origin}」。已联网向 Kiwi 查询仍未找到。"
            f"建议改用机场三字码（如 PEK / MXP / LJU）或英文名（如 Milan）；"
            f"部分小城市的中文名 Kiwi 不认，英文名更可靠。"
        )
    if not d:
        raise ValueError(
            f"无法识别到达城市「{destination}」。已联网向 Kiwi 查询仍未找到。"
            f"建议改用机场三字码（如 MXP / JFK / LJU）或英文名（如 Milan）；"
            f"部分小城市的中文名 Kiwi 不认，英文名更可靠。"
        )
    if o == d:
        raise ValueError("出发和到达是同一个城市")
    try:
        _date.fromisoformat(date)
    except ValueError as e:
        raise ValueError(f"日期格式应为 YYYY-MM-DD：{date}") from e

    resolved_hubs: list[str] = []
    for h in hubs or []:
        code = airports.resolve_city(h)
        if not code and allow_remote:
            code = _resolve_remote(h, currency)
        if code and code not in resolved_hubs and code not in (o, d):
            resolved_hubs.append(code)

    return Query(
        origin=o,
        destination=d,
        date=date,
        date_window=max(0, min(int(date_window), MAX_DATE_WINDOW)),
        adults=max(1, int(adults)),
        currency=currency.upper(),
        hubs=resolved_hubs,
        max_ground_min=int(max_ground_min),
        max_stops=int(max_stops),
        return_date=(return_date or "").strip(),
    )


def make_provider(q: Query, offline: bool = False, refresh: bool = False):
    if offline:
        return FixtureProvider(currency=q.currency, max_stops=q.max_stops)
    return KiwiProvider(
        currency=q.currency,
        adults=q.adults,
        max_stops=q.max_stops,
        max_duration_min=q.max_ground_min,
        min_interval=0.30,
        # refresh=True：跳过磁盘缓存，每次都是刚从 Kiwi 拉的新价
        cache_ttl=0.0 if refresh else 1800.0,
    )


def _dates(q: Query) -> list[str]:
    base = _date.fromisoformat(q.date)
    out = []
    for delta in range(-q.date_window, q.date_window + 1):
        out.append((base + timedelta(days=delta)).isoformat())
    return out


def _prefetch(provider, q: Query, hubs: list[str], progress: Optional[Callable] = None,
              workers: int = 6) -> None:
    """把 (城市A->城市B, 日期, 舱位) 的查询先并发打热缓存，engine 侧就只剩组装。"""
    dates = _dates(q)
    tasks = []
    for hub in hubs:
        for a, b in ((q.origin, hub), (hub, q.destination)):
            for d in dates:
                for c in CABINS_TO_PRICE:
                    tasks.append((a, b, d, c))
    total = len(tasks)
    done = 0
    if progress:
        progress(0, total, "预取报价")

    def one(t):
        try:
            return provider.legs(*t)
        except Exception:  # noqa: BLE001
            return []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in ex.map(one, tasks):
            done += 1
            if progress and done % 4 == 0:
                progress(done, total, "预取报价")
    if progress:
        progress(total, total, "预取完成")


def _prefetch_many(provider, queries: list, hubs: list[str], workers: int = 8) -> None:
    """把多个方向的热身任务合并到一个线程池里跑，避免串行两轮。"""
    tasks = []
    for q in queries:
        dates = _dates(q)
        for hub in hubs:
            for a, b in ((q.origin, hub), (hub, q.destination)):
                for d in dates:
                    for c in CABINS_TO_PRICE:
                        tasks.append((a, b, d, c))

    def one(t):
        try:
            return provider.legs(*t)
        except Exception:  # noqa: BLE001
            return []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, tasks))


def _brief_entry(best, label: str) -> Optional[dict]:
    if not best:
        return None
    return {
        "price": round(best.price, 2),
        "duration_min": best.duration_min,
        "stops": best.stops,
        "label": label,
        "cabins": sorted(best.cabins),
        "depart_local": best.depart_local,
        "airlines": sorted({s.carrier for s in best.segments if s.carrier}),
    }


def _cheapest_leg(provider, origin, dest, date, cabin, max_stops=None):
    best = None
    for leg in provider.legs(origin, dest, date, cabin, max_stops=max_stops)[:2]:
        if best is None or leg.price < best.price:
            best = leg
    return best


def _baseline(provider, q: Query) -> dict:
    """整程直搜的对比锚：最低价（可能中转） + 直飞（无中转）。"""
    out = {}
    for key, cabin, cn in (
        ("through_economy", "ECONOMY", "经济舱"),
        ("through_business", "BUSINESS", "公务舱"),
    ):
        best = None
        for d in _dates(q):
            for leg in provider.legs(q.origin, q.destination, d, cabin)[:2]:
                if best is None or leg.price < best.price:
                    best = leg
        out[key] = _brief_entry(best, f"整程{cn}（平台直搜最低，可能中转）")

    # 直飞：maxStopsCount=0，整程不中转。没有直飞航线时该字段为 None。
    for key, cabin, cn in (
        ("nonstop_economy", "ECONOMY", "经济舱"),
        ("nonstop_business", "BUSINESS", "公务舱"),
    ):
        best = None
        for d in _dates(q):
            leg = _cheapest_leg(provider, q.origin, q.destination, d, cabin, max_stops=0)
            if leg and (best is None or leg.price < best.price):
                best = leg
        out[key] = _brief_entry(best, f"整程直飞{cn}（无中转）")
    return out


def _direct_comment(baseline: dict) -> Optional[str]:
    """把「直飞 vs 最低中转」的差价与时间差说清楚。"""
    ne = baseline.get("nonstop_economy")
    te = baseline.get("through_economy")
    if not ne or not te:
        return None
    diff = ne["price"] - te["price"]
    faster = te["duration_min"] - ne["duration_min"]
    if ne["stops"] == te["stops"] and abs(diff) < 1:
        return f"这条航线直飞就是最低价：{ne['duration_min'] // 60} 小时，¥{ne['price']:,.0f}。"
    time_part = f"，但省 {faster // 60} 小时" if faster > 15 else ""
    if diff >= 0:
        return (
            f"直飞 {ne['duration_min'] // 60} 小时、¥{ne['price']:,.0f}；"
            f"比最低中转贵 ¥{diff:,.0f}{time_part}"
            f"（中转全程 {te['duration_min'] // 60} 小时）。"
        )
    return (
        f"直飞 ¥{ne['price']:,.0f} 反而比最低中转（¥{te['price']:,.0f}）便宜 ¥{-diff:,.0f}"
        f"{'，还省 ' + str(faster // 60) + ' 小时' if faster > 15 else ''}，没理由中转。"
    )


def _rest_block(leg_eco, leg_biz) -> dict:
    """给一段航线的休息价值说明（供界面展示）。

    口径说明：
      * `sleep_h` / `night_ratio` 以「经济舱班次」为参照（升舱前你本来会坐的那班）；
      * `effective_rest_h` 分别按各自舱位**实际班次**计算，和 optimize 的组合口径一致；
      * 经济舱/公务舱若取到不同班次，`different_flight=True`，此时差价买的不只是舱位，
        还换了一个时间更好的班次，界面上要解释清楚，否则会误导。
    """
    rest = {
        "sleep_h": round(score.sleep_window_h(leg_eco), 2) if leg_eco else 0.0,
        "night_ratio": round(score.night_ratio(leg_eco), 3) if leg_eco else 0.0,
        "effective_rest_h": {},
        "score": 0.0,
        "verdict": "该段暂无经济舱报价",
        "cost_per_rest_h": None,
        "different_flight": False,
        "upgrade_note": None,
    }
    for cabin, leg in (("ECONOMY", leg_eco), ("BUSINESS", leg_biz)):
        if leg:
            rest["effective_rest_h"][cabin] = round(score.effective_rest_h(leg, cabin), 2)
    if leg_eco:
        reference = leg_biz or leg_eco
        rest["score"] = round(score.rest_score(reference, "BUSINESS"), 1)
        if rest["score"] >= 55:
            rest["verdict"] = "夜间长途，升公务舱的休息收益最大"
        elif rest["score"] >= 35:
            rest["verdict"] = "有一定睡眠窗口，可按预算考虑"
        else:
            rest["verdict"] = "以白天航段为主，升舱休息收益有限"
    if leg_eco and leg_biz:
        cpr = score.cost_per_rest_hour(leg_eco, leg_biz)
        rest["cost_per_rest_h"] = round(cpr, 0) if cpr is not None else None
        same_flight = (
            leg_eco.depart_local == leg_biz.depart_local
            and [s.flight_no for s in leg_eco.segments] == [s.flight_no for s in leg_biz.segments]
        )
        rest["different_flight"] = not same_flight
        if not same_flight:
            rest["upgrade_note"] = (
                f"注意：这一段的经济舱（{leg_eco.depart_local[5:16]} 起飞）与公务舱"
                f"（{leg_biz.depart_local[5:16]} 起飞）不是同一个班次，差价里包含了换班次的成本。"
            )
    return rest


def _plan_dict(plan) -> dict:
    d = plan.to_dict()
    d["hub_name"] = airports.city_name(plan.hub)
    annotation = optimize.annotate(plan)
    combos = annotation.get("combos") or []
    d["combos"] = [c.to_dict() if hasattr(c, "to_dict") else c for c in combos]
    d["best_value"] = annotation.get("best_value")
    d["pareto"] = annotation.get("pareto") or []
    for i, leg_dict in enumerate(d["legs"]):
        choice = plan.legs[i]
        leg_dict["rest"] = _rest_block(choice.options.get("ECONOMY"), choice.options.get("BUSINESS"))
    return d


def _cheapest(legs):
    base = [l for l in (legs or []) if l and l.price > 0]
    return min(base, key=lambda l: l.price) if base else None


def _plan_summary(plan):
    """取一个拆分方案里最便宜的全经济舱组合 + 最值得升的一段。"""
    if plan is None:
        return None
    ann = optimize.annotate(plan)
    combos = ann.get("combos") or []
    if not combos:
        return None
    eco = combos[0]
    return {
        "plan": plan,
        "hub": plan.hub,
        "price": float(eco.get("total_price") or 0),
        "duration_min": float(eco.get("total_duration_min") or 0),
        "cabins": list(eco.get("cabins") or []),
        "best_value": ann.get("best_value"),
    }


def _best_split(sums):
    known = [s for s in sums if s and s["price"] > 0]
    return min(known, key=lambda s: s["price"]) if known else None


def run_roundtrip(
    q: Query,
    offline: bool = False,
    max_hubs: int = DEFAULT_MAX_HUBS,
    progress: Optional[Callable[[int, int, str], None]] = None,
    refresh: bool = False,
) -> dict:
    """往返整合：对比「一张往返票」「两张单程票」「去/回程分别拆段」等购买方式。"""
    if not q.return_date:
        return run(q, offline=offline, max_hubs=max_hubs, progress=progress, refresh=refresh)

    started = time.time()
    provider = make_provider(q, offline=offline, refresh=refresh)
    errors: list[str] = []

    # 往返要打两倍的请求，默认少评估一个枢纽，保证 1-2 分钟内出结果
    hubs = split.candidate_hubs(q)[: max(1, min(max_hubs, 3))]
    q_ret = Query(
        origin=q.destination,
        destination=q.origin,
        date=q.return_date,
        date_window=q.date_window,
        adults=q.adults,
        currency=q.currency,
        hubs=list(hubs),
        max_ground_min=q.max_ground_min,
        max_stops=q.max_stops,
    )

    # ---------- 预取两个方向的报价 ----------
    if progress:
        progress(0, 1, "预取往返报价")
    try:
        provider.return_trips(q.origin, q.destination, q.date, q.return_date, "ECONOMY")
        provider.return_trips(q.origin, q.destination, q.date, q.return_date, "BUSINESS")
        # 两个方向一起并行预热，8 并发
        _prefetch_many(provider, [q, q_ret], hubs, workers=8)
    except Exception as e:  # noqa: BLE001
        errors.append(f"部分报价预取失败：{type(e).__name__}: {e}")

    # ---------- 拆分方案 ----------
    if progress:
        progress(0, 2, "组装去程拆分方案")
    plans_out = split.build_plans(q, provider.legs, hubs=hubs)
    if progress:
        progress(1, 2, "组装回程拆分方案")
    plans_ret = split.build_plans(q_ret, provider.legs, hubs=hubs)

    split_out = _best_split([_plan_summary(p) for p in plans_out])
    split_ret = _best_split([_plan_summary(p) for p in plans_ret])

    # ---------- 往返票 / 两张单程票 ----------
    rt_eco = _cheapest_rt(provider.return_trips(q.origin, q.destination, q.date, q.return_date, "ECONOMY"))
    rt_biz = _cheapest_rt(provider.return_trips(q.origin, q.destination, q.date, q.return_date, "BUSINESS"))
    one_out_eco = _cheapest(provider.legs(q.origin, q.destination, q.date, "ECONOMY"))
    one_ret_eco = _cheapest(provider.legs(q.destination, q.origin, q.return_date, "ECONOMY"))
    one_out_biz = _cheapest(provider.legs(q.origin, q.destination, q.date, "BUSINESS"))
    one_ret_biz = _cheapest(provider.legs(q.destination, q.origin, q.return_date, "BUSINESS"))

    # ---------- 直飞（去回程都不中转）----------
    rt_direct_eco = _cheapest_rt(
        provider.return_trips(q.origin, q.destination, q.date, q.return_date, "ECONOMY", max_stops=0))
    rt_direct_biz = _cheapest_rt(
        provider.return_trips(q.origin, q.destination, q.date, q.return_date, "BUSINESS", max_stops=0))
    out_direct_eco = _cheapest_leg(provider, q.origin, q.destination, q.date, "ECONOMY", max_stops=0)
    ret_direct_eco = _cheapest_leg(provider, q.destination, q.origin, q.return_date, "ECONOMY", max_stops=0)
    out_direct_biz = _cheapest_leg(provider, q.origin, q.destination, q.date, "BUSINESS", max_stops=0)
    ret_direct_biz = _cheapest_leg(provider, q.destination, q.origin, q.return_date, "BUSINESS", max_stops=0)

    strategies: list[dict] = []

    def add(key, label, price, duration, cabin, seller, note="", recommended=False):
        if price is None or price <= 0:
            return
        strategies.append({
            "key": key, "label": label, "price": round(float(price), 2),
            "currency": q.currency, "duration_min": round(float(duration or 0)),
            "cabin": cabin, "cabin_cn": CABIN_CN.get(cabin, cabin),
            "seller": seller, "note": note, "recommended": recommended,
        })

    if rt_eco:
        add("rt_economy", "一张往返票 · 经济舱", rt_eco.price, rt_eco.duration_min,
            "ECONOMY", "航司/OTA 往返票", rt_eco.note)
    if rt_direct_eco:
        add("rt_direct_economy", "直飞往返票 · 经济舱", rt_direct_eco.price, rt_direct_eco.duration_min,
            "ECONOMY", "航司/OTA 往返票", "去回程都不中转；整程同一舱位")
    if out_direct_eco and ret_direct_eco:
        add("two_direct_oneway_economy", "两张直飞单程票 · 经济舱",
            out_direct_eco.price + ret_direct_eco.price,
            out_direct_eco.duration_min + ret_direct_eco.duration_min,
            "ECONOMY", "两张直飞单程", "去回程各自直飞、分开买")
    if rt_biz:
        add("rt_business", "一张往返票 · 公务舱", rt_biz.price, rt_biz.duration_min,
            "BUSINESS", "航司/OTA 往返票", rt_biz.note)
    if rt_direct_biz:
        add("rt_direct_business", "直飞往返票 · 公务舱", rt_direct_biz.price, rt_direct_biz.duration_min,
            "BUSINESS", "航司/OTA 往返票", "去回程都不中转；整程同一舱位")
    if out_direct_biz and ret_direct_biz:
        add("two_direct_oneway_business", "两张直飞单程票 · 公务舱",
            out_direct_biz.price + ret_direct_biz.price,
            out_direct_biz.duration_min + ret_direct_biz.duration_min,
            "BUSINESS", "两张直飞单程", "去回程各自直飞、分开买")
    if one_out_eco and one_ret_eco:
        add("two_oneway_economy", "两张整程单程票 · 经济舱",
            one_out_eco.price + one_ret_eco.price,
            one_out_eco.duration_min + one_ret_eco.duration_min,
            "ECONOMY", "两段分开买", "去程、回程各一张，互不牵连")
    if one_out_biz and one_ret_biz:
        add("two_oneway_business", "两张整程单程票 · 公务舱",
            one_out_biz.price + one_ret_biz.price,
            one_out_biz.duration_min + one_ret_biz.duration_min,
            "BUSINESS", "两段分开买")
    if split_out and one_ret_eco:
        add("out_split", "去程拆段 + 回程整程 · 经济舱",
            split_out["price"] + one_ret_eco.price,
            split_out["duration_min"] + one_ret_eco.duration_min,
            "ECONOMY", "去程分段买", f"在 {airports.city_name(split_out['hub'])} 拆去程")
    if split_ret and one_out_eco:
        add("ret_split", "回程拆段 + 去程整程 · 经济舱",
            split_ret["price"] + one_out_eco.price,
            split_ret["duration_min"] + one_out_eco.duration_min,
            "ECONOMY", "回程分段买", f"在 {airports.city_name(split_ret['hub'])} 拆回程")
    if split_out and split_ret:
        add("both_split", "去程、回程都拆段 · 全经济舱",
            split_out["price"] + split_ret["price"],
            split_out["duration_min"] + split_ret["duration_min"],
            "ECONOMY", "四张单程票",
            f"去程 {airports.city_name(split_out['hub'])} / 回程 {airports.city_name(split_ret['hub'])}")

    # 最值得升的一段（去程/回程里 cost_per_rest_hour 最小且合格）
    upgrades = []
    for direction, summ in (("out", split_out), ("ret", split_ret)):
        bv = (summ or {}).get("best_value") or {}
        if bv.get("cost_per_rest_h") and bv.get("extra_rest_h", 0) >= 1.0:
            upgrades.append({"direction": direction, **bv})
    best_upgrade = min(upgrades, key=lambda u: u["cost_per_rest_h"]) if upgrades else None

    if best_upgrade and split_out and split_ret:
        base_price = split_out["price"] + split_ret["price"]
        add("both_split_upgrade",
            "去程、回程都拆段 · 只把最值的一段升公务舱",
            base_price + best_upgrade.get("extra_cost", 0),
            float(split_out["duration_min"] + split_ret["duration_min"] + best_upgrade.get("extra_duration_min", 0)),
            "MIXED", "四张单程票 + 1 段升舱",
            f"升{'去程' if best_upgrade['direction'] == 'out' else '回程'}第 "
            f"{int(best_upgrade.get('leg_index', 0)) + 1} 段：多花 ¥{best_upgrade.get('extra_cost', 0):,.0f} "
            f"换 {best_upgrade.get('extra_rest_h', 0):.1f}h（¥{best_upgrade.get('cost_per_rest_h', 0):,.0f}/h）")

    strategies.sort(key=lambda s: s["price"])
    if strategies:
        strategies[0]["is_cheapest"] = True
        for s in strategies:
            s["delta_vs_cheapest"] = round(s["price"] - strategies[0]["price"], 2)

    # ---------- 推荐 ----------
    cheapest = strategies[0] if strategies else None
    rt_saving = None
    if rt_eco and one_out_eco and one_ret_eco:
        rt_saving = (one_out_eco.price + one_ret_eco.price) - rt_eco.price

    reasons = []
    if rt_saving is not None:
        if rt_saving > 0:
            reasons.append(
                f"往返票（¥{rt_eco.price:,.0f}）比两张整程单程票"
                f"（¥{one_out_eco.price + one_ret_eco.price:,.0f}）便宜 ¥{rt_saving:,.0f}。")
        else:
            reasons.append(
                f"这张往返票（¥{rt_eco.price:,.0f}）并不比两张单程票便宜，"
                f"反而贵 ¥{-rt_saving:,.0f}，可以分开买。")
    if split_out and cheapest:
        reasons.append(
            f"去程在 {airports.city_name(split_out['hub'])} 拆段后全经济舱 ¥{split_out['price']:,.0f}，"
            f"全程 {split_out['duration_min'] / 60:.1f} 小时。")
    if split_ret:
        reasons.append(
            f"回程在 {airports.city_name(split_ret['hub'])} 拆段后全经济舱 ¥{split_ret['price']:,.0f}。")
    if best_upgrade:
        reasons.append(
            f"最值得升舱的是{'去程' if best_upgrade['direction'] == 'out' else '回程'}第 "
            f"{int(best_upgrade.get('leg_index', 0)) + 1} 段：多花 ¥{best_upgrade.get('extra_cost', 0):,.0f} "
            f"换 {best_upgrade.get('extra_rest_h', 0):.1f} 小时有效休息"
            f"（约 ¥{best_upgrade.get('cost_per_rest_h', 0):,.0f}/小时）。")
    elif split_out or split_ret:
        reasons.append("这一段往返行程里没有「多睡 1 小时还算划算」的升舱选项，建议全经济舱。")

    # 直飞这条基准线：和拆段/中转方案直接对比
    if rt_direct_eco:
        base_oneway = None
        if one_out_eco and one_ret_eco:
            base_oneway = one_out_eco.price + one_ret_eco.price
        if base_oneway:
            delta = rt_direct_eco.price - base_oneway
            saved_h = (one_out_eco.duration_min + one_ret_eco.duration_min
                       - rt_direct_eco.duration_min) / 60.0
            time_part = f"，还少花 {saved_h:.1f} 小时" if saved_h > 0.3 else ""
            if delta >= 0:
                reasons.append(
                    f"直飞往返票 ¥{rt_direct_eco.price:,.0f}，比两张中转单程"
                    f"（¥{base_oneway:,.0f}）贵 ¥{delta:,.0f}{time_part}。")
            else:
                reasons.append(
                    f"直飞往返票 ¥{rt_direct_eco.price:,.0f} 反而比两张中转单程便宜 ¥{-delta:,.0f}"
                    f"{time_part}。")
    else:
        reasons.append("这条航线没有可用的直飞往返票（去回程都要直飞），只能在中转方案里选。")

    if cheapest:
        headline = (
            f"最省的买法是「{cheapest['label']}」：¥{cheapest['price']:,.0f}，"
            f"全程 {cheapest['duration_min'] / 60:.1f} 小时。"
        )
    else:
        headline = "没有拿到可用的往返报价，请换日期或枢纽再试。"

    payload = {
        "ok": bool(strategies),
        "mode": "roundtrip",
        "query": {
            **q.to_dict(),
            "origin_name": airports.city_name(q.origin),
            "destination_name": airports.city_name(q.destination),
            "hubs": hubs,
            "hubs_name": [airports.city_name(h) for h in hubs],
        },
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "prices_from_cache": not refresh,
        "source": {"name": provider.name, "live": provider.live, "refresh": bool(refresh)},
        "price_note": PRICE_NOTE if provider.live else "示例数据：价格由本地模型合成，仅用于演示。",
        "strategies": strategies,
        "recommendation": {
            "headline": headline,
            "reasons": reasons[:5],
            "strategy": cheapest,
            "upgrade": best_upgrade,
            "warnings": [],
        },
        "baseline": {
            "return_ticket_economy": _rt_brief(rt_eco),
            "return_ticket_business": _rt_brief(rt_biz),
            "nonstop_return_economy": _rt_brief(rt_direct_eco),
            "nonstop_return_business": _rt_brief(rt_direct_biz),
            "oneway_out_economy": _leg_brief(one_out_eco),
            "oneway_ret_economy": _leg_brief(one_ret_eco),
            "nonstop_out_economy": _leg_brief(out_direct_eco),
            "nonstop_ret_economy": _leg_brief(ret_direct_eco),
        },
        "plans": [_plan_dict(p) for p in plans_out if p.legs],
        "ret_plans": [_plan_dict(p) for p in plans_ret if p.legs],
        "warnings": list(WARNINGS) + [
            "往返票（一张票）通常比两张单程便宜，但整程只有一个舱位，不能只升其中一段。",
            "分段买票两边都是独立票：任一段延误都不保护另一段，行李可能都要重新托运。",
        ],
        "errors": errors,
    }
    elapsed = time.time() - started
    payload["elapsed_sec"] = round(elapsed, 1)
    if not strategies:
        payload["errors"].append("没有拼出可用的往返方案：换日期或枢纽再试。")
    return payload


def _cheapest_rt(trips):
    return min(trips, key=lambda r: (r.price, r.duration_min)) if trips else None


def _rt_brief(rt):
    if not rt:
        return None
    return {
        "price": round(rt.price, 2),
        "currency": rt.currency,
        "cabin": rt.cabin,
        "cabin_cn": CABIN_CN.get(rt.cabin, rt.cabin),
        "duration_min": rt.duration_min,
        "outbound_min": rt.outbound.duration_min,
        "inbound_min": rt.inbound.duration_min,
        "note": rt.note,
    }


def _leg_brief(leg):
    if not leg:
        return None
    return {
        "price": round(leg.price, 2),
        "currency": leg.currency,
        "duration_min": leg.duration_min,
        "stops": leg.stops,
        "depart_local": leg.depart_local,
        "arrive_local": leg.arrive_local,
        "cabin": leg.cabin,
    }


def run(
    q: Query,
    offline: bool = False,
    max_hubs: int = DEFAULT_MAX_HUBS,
    progress: Optional[Callable[[int, int, str], None]] = None,
    refresh: bool = False,
) -> dict:
    """执行一次完整分析（单程），返回 SPEC §6 约定的 JSON。

    refresh=True 会跳过本地价格缓存，每次查询都实时向 Kiwi 取数（更慢但更新）。
    """
    started = time.time()
    provider = make_provider(q, offline=offline, refresh=refresh)
    errors: list[str] = []
    payload = {
        "ok": True,
        "query": {
            **q.to_dict(),
            "origin_name": airports.city_name(q.origin),
            "destination_name": airports.city_name(q.destination),
        },
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "prices_from_cache": not refresh,
        "source": {"name": provider.name, "live": provider.live, "refresh": bool(refresh)},
        "baseline": {},
        "plans": [],
        "recommendation": {},
        "price_note": PRICE_NOTE if provider.live else "示例数据：价格由本地模型合成，只用于演示口径与界面，不代表任何真实报价。",
        "warnings": list(WARNINGS),
        "notes": [
            "同一段航线的「公务舱价」是整段（含中转）全公务舱的最低枚举价。",
            "组合总价 = 各段报价之和，不含行李/选座等附加费；实际购票需按段分别下单。",
            f"本次共查询 {max_hubs} 个候选枢纽，耗时约 {0:.0f} 秒。",
        ],
        "errors": errors,
    }

    hubs = split.candidate_hubs(q)[: max(1, max_hubs)]
    if not hubs:
        payload["ok"] = False
        errors.append("找不到可用的拆分枢纽，请手动指定 --hubs（例如 IST,DOH,DXB）。")
        return payload
    payload["query"]["hubs"] = hubs
    payload["query"]["hubs_name"] = [airports.city_name(h) for h in hubs]

    try:
        _prefetch(provider, q, hubs, progress)
    except Exception as e:  # noqa: BLE001  预取失败不影响主流程（engine 会自己拉）
        errors.append(f"预取部分报价失败：{type(e).__name__}: {e}")

    if progress:
        progress(0, 1, "组装拆分方案")
    plans = split.build_plans(q, provider.legs, hubs=hubs, progress=progress)
    payload["baseline"] = _baseline(provider, q)

    first_legs = [p for p in plans if p.legs]
    payload["plans"] = [_plan_dict(p) for p in first_legs]

    try:
        payload["recommendation"] = optimize.recommend(plans, payload["baseline"])
    except Exception as e:  # noqa: BLE001
        errors.append(f"推荐计算失败：{type(e).__name__}: {e}")
        payload["recommendation"] = {"headline": "方案已生成，但推荐结论计算失败。", "reasons": [], "warnings": []}

    # 把「直飞」这条基准线写进结论，方便和拆段方案直接对比
    direct_note = _direct_comment(payload["baseline"])
    if direct_note:
        payload["direct_note"] = direct_note
        rec = payload["recommendation"]
        rec["reasons"] = [direct_note] + list(rec.get("reasons") or [])
        rec["reasons"] = rec["reasons"][:5]

    if not payload["plans"]:
        payload["ok"] = False
        errors.append("没有拼出可行的分段方案：可能这两城之间航班太少，或换一个枢纽试试。")

    getattr_errors = getattr(provider, "last_errors", None)
    if getattr_errors:
        errors.extend(str(e) for e in getattr_errors[:5])

    elapsed = time.time() - started
    payload["notes"][-1] = f"本次共查询 {len(hubs)} 个候选枢纽，耗时约 {elapsed:.0f} 秒。"
    payload["elapsed_sec"] = round(elapsed, 1)
    return payload

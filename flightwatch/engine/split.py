"""候选枢纽与拆分方案生成（SPEC §5.1 / §5.2）。

本模块只做「本地组装」：所有报价都来自注入的 fetch 回调
    fetch(origin, dest, date, cabin) -> list[Leg]
不会直接联网（缓存/限速由 provider 负责，见 providers/）。

单位约定：
  * 距离：km（直线距离，haversine 粗算）
  * 时间：分钟 min；衔接窗口 [90, 1200] min（1.5h ~ 20h）
  * 价格：CNY（元）

主要常量集中写在文件头，便于整体调参而不改语义。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional

from .. import airports
from ..models import Leg, LegChoice, Query, SplitPlan, parse_dt

# --------------------------------------------------------------------------- 可调参数
# 同枢纽两段票的最小衔接（min）：90 分钟 = 下机 + 步行/摆渡 + 重新安检的保守余量。
# 分段票不是联程票，误机自己承担，所以不能比 90 分钟更乐观。
MIN_CONNECT_MIN = 90
# 最大衔接（min）：20 小时。再长不如在中转地住一晚，行李和误机风险都更高。
MAX_CONNECT_MIN = 20 * 60
# 单段直线距离下限（km）：小于 300km 拆票毫无意义（地面交通更快，且省不了钱）。
MIN_LEG_DISTANCE_KM = 300.0
# 绕行比上限：(d(o,h)+d(h,d)) / d(o,d)。1.6 = 比直飞多飞 60% 以内才值得拆。
MAX_DETOUR_RATIO = 1.6
# 整程短于 500km 时不检查绕行比：短途的绕行比天然很大，但票价/班次仍可能划算。
SHORT_ROUTE_KM = 500.0
# 默认候选枢纽数量上限（SPEC §5.1「默认最多取 4 个枢纽」）。
MAX_DEFAULT_HUBS = 4

# 必查舱位：经济舱是底价锚点，公务舱是「升舱」主角（SPEC §5.2）。
REQUIRED_CABINS = ("ECONOMY", "BUSINESS")
# 超级经济舱：v1 默认关闭。用户的决策问题就是「经济舱 vs 公务舱」二选一，
# 超经属于噪音；且 live 模式每多一个舱位会多 16 次请求（+8~15s）。
# 代码路径保留完整：把它改成 True 即可枚举超经组合（api._prefetch 未预热时会慢一些）。
ENABLE_PREMIUM_ECONOMY = False
OPTIONAL_CABINS = ("PREMIUM_ECONOMY",) if ENABLE_PREMIUM_ECONOMY else ()

# 每段查询的日期偏移（天）：出发日当天 + 次日。
# 跨境长航线常见「次日到达」，第二段必须允许次日出发才拼得上（SPEC §5.1）。
DATE_OFFSETS = (0, 1)

ProgressCallback = Optional[Callable[[int, int, str], None]]


# --------------------------------------------------------------------------- 小工具
def _code(value) -> str:
    """把用户输入（中文名/英文名/三字码）归一化成城市码；无法识别时退化为大写原串。"""
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    return airports.resolve_city(raw) or raw.upper()


def _add_days(date_str: str, days: int) -> str:
    """'YYYY-MM-DD' + N 天，返回 'YYYY-MM-DD'。日期非法时抛 ValueError。"""
    try:
        base = datetime.strptime(str(date_str), "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"date 必须是 YYYY-MM-DD：{date_str!r}") from exc
    return (base + timedelta(days=days)).isoformat()


def _wall_clock_minutes(begin: str, end: str) -> Optional[float]:
    """同一地点（同枢纽）两个墙钟时间之间的分钟数；解析失败返回 None。

    两段票都在 hub 落地/起飞，属于同一时区，所以直接按时钟差算即可，
    不能把其中一边 convert 到别的时区。
    """
    a = _local_naive(begin)
    b = _local_naive(end)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 60.0


def _local_naive(value: str) -> Optional[datetime]:
    """ISO 时间串 -> 无时区墙钟 datetime（与 score.py 同口径）。"""
    if not value:
        return None
    try:
        dt = parse_dt(value)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _connection_min(leg1: Leg, leg2: Leg) -> Optional[float]:
    """两段票的衔接时长（min），只有落在 [90, 1200] 内才返回，否则 None。

    leg1.arrive_local 与 leg2.depart_local 都在 hub 的当地时区，可直接相减。
    """
    gap = _wall_clock_minutes(leg1.arrive_local, leg2.depart_local)
    if gap is None:
        return None
    if MIN_CONNECT_MIN <= gap <= MAX_CONNECT_MIN:
        return gap
    return None


def _price_key(leg: Leg) -> tuple:
    """票段选择用的稳定排序键：先价格、再总时长、再起飞时间。"""
    try:
        price = float(leg.price)
    except (TypeError, ValueError):
        price = float("inf")
    return (round(price, 2), float(leg.duration_min or 0.0), str(leg.depart_local or ""))


def _clean_legs(legs: Iterable[Leg], q: Query) -> list[Leg]:
    """过滤掉明显不可用的票段（脏数据 / 超出查询约束）。

    约束来源是 models.Query 的字段语义：
      * max_stops：每个票段最多中转几次；
      * allow_self_transfer：是否接受「自己重新托运行李」的伪联程。
    注意 max_ground_min 在 models 里的注释是「单段行程最长总时长」，provider 已用它
    过滤过报价，engine 不再二次使用（SPEC §5.2 的衔接窗口固定为 90min~20h）。
    """
    max_stops = getattr(q, "max_stops", 1)
    allow_self_transfer = getattr(q, "allow_self_transfer", True)
    cleaned: list[Leg] = []
    for leg in legs:
        if not isinstance(leg, Leg):
            continue
        try:
            price = float(leg.price)
        except (TypeError, ValueError):
            continue
        if price <= 0:  # 没有有效报价
            continue
        try:
            duration = float(leg.duration_min or 0.0)
        except (TypeError, ValueError):
            continue
        if duration <= 0:
            continue
        stops = leg.stops or 0
        if max_stops is not None and stops > max_stops:
            continue
        if not allow_self_transfer and getattr(leg, "self_transfer", False):
            continue
        cleaned.append(leg)
    return cleaned


def _dedupe_legs(legs: Iterable[Leg]) -> list[Leg]:
    """去重（不同日期查询可能召回同一条票），保持原始顺序。"""
    seen: set[tuple] = set()
    out: list[Leg] = []
    for leg in legs:
        key = (
            leg.origin,
            leg.destination,
            leg.depart_local,
            leg.arrive_local,
            leg.cabin,
            round(float(leg.price or 0.0), 2),
            leg.stops or 0,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(leg)
    return out


def _arrival_airport(leg: Leg) -> str:
    """票段最后一段的落地机场三字码（没有 segments 时退回城市码）。"""
    if leg.segments:
        code = str(getattr(leg.segments[-1], "destination", "") or "")
        if code:
            return code.upper()
    return str(leg.destination or "").upper()


def _departure_airport(leg: Leg) -> str:
    """票段第一段的起飞机场三字码（没有 segments 时退回城市码）。"""
    if leg.segments:
        code = str(getattr(leg.segments[0], "origin", "") or "")
        if code:
            return code.upper()
    return str(leg.origin or "").upper()


def _build_note(hub: str, leg1: Leg, leg2: Leg) -> str:
    """生成前端展示用的换乘说明（SPEC §5.2 要求写明换机场/航站楼/行李）。"""
    parts: list[str] = []
    if hub in airports.SINGLE_AIRPORT_HUBS:
        # 已知单机场枢纽，可以明确告诉用户「同一机场」
        parts.append("同一机场中转")
    else:
        arrive_airport = _arrival_airport(leg1)
        depart_airport = _departure_airport(leg2)
        if arrive_airport and depart_airport and arrive_airport != depart_airport:
            parts.append(
                f"需换机场中转（{arrive_airport}→{depart_airport}），"
                "两机场间地面转移请额外预留 2 小时以上"
            )
        elif arrive_airport and depart_airport:
            parts.append("同一机场中转")
        else:
            parts.append("是否同一机场不可知，请按换机场预案预留时间")
    parts.append("航站楼是否相同不可知")
    parts.append("行李通常需取出重新托运并重新安检")
    return "；".join(parts)


def _sanitize_hub_list(q: Query, hubs: Iterable[str], *, limit: Optional[int]) -> list[str]:
    """枢纽列表归一化 + SPEC §5.1 过滤 + 按绕行比升序排序。

    过滤规则（适用于默认枢纽池与用户显式指定的枢纽）：
      1. 不能等于出发/到达城市；
      2. 两段直线距离都 >= 300km；
      3. 绕行比 <= 1.6（整程 < 500km 时跳过此项）。
    `limit` 为 None 表示不截断（用户显式指定枢纽时尊重用户选择）。
    """
    origin = _code(q.origin)
    destination = _code(q.destination)
    if not origin or not destination:
        return []
    direct_km = airports.distance_km(origin, destination)

    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for raw in hubs or []:
        hub = _code(raw)
        if not hub or hub in seen:
            continue
        if hub in (origin, destination):
            continue
        leg1_km = airports.distance_km(origin, hub)
        leg2_km = airports.distance_km(hub, destination)
        if leg1_km < MIN_LEG_DISTANCE_KM or leg2_km < MIN_LEG_DISTANCE_KM:
            continue
        if direct_km >= SHORT_ROUTE_KM:
            ratio = (leg1_km + leg2_km) / direct_km
            if ratio > MAX_DETOUR_RATIO:
                continue
        else:
            # 短途跳过绕行检查；没有 ratio 可以排序，放到最后（inf）
            ratio = float("inf")
        seen.add(hub)
        scored.append((ratio, hub))

    scored.sort(key=lambda item: (item[0], item[1]))
    ordered = [hub for _, hub in scored]
    return ordered[:limit] if limit is not None else ordered


# --------------------------------------------------------------------------- 公开接口
def candidate_hubs(q: Query) -> list[str]:
    """返回候选拆分枢纽城市码列表（SPEC §5.1）。

    * `q.hubs` 非空：只在这些枢纽里筛（尊重用户指定，不截断数量）；
    * `q.hubs` 为空：用 airports.DEFAULT_HUBS 全量池，按绕行比升序取前 4 个。

    返回的都是归一化后的城市码（如 "IST"），已过滤掉与出发/到达同城、
    单段距离 < 300km、绕行比 > 1.6 的枢纽。
    """
    explicit = list(getattr(q, "hubs", None) or [])
    if explicit:
        return _sanitize_hub_list(q, explicit, limit=None)
    return _sanitize_hub_list(q, airports.DEFAULT_HUBS, limit=MAX_DEFAULT_HUBS)


def _report_progress(progress: ProgressCallback, done: int, total: int, label: str) -> None:
    """调用进度回调 progress(done, total, label)；回调异常不打断主流程。

    api.py 约定签名为 progress(done: int, total: int, label: str)，
    Web 端靠它显示「预取/组装」进度条。
    """
    if progress is None:
        return
    try:
        progress(done, total, label)
    except Exception:  # noqa: BLE001  进度展示失败绝不能影响计算结果
        pass


def build_plan(q: Query, hub: str, fetch) -> Optional[SplitPlan]:
    """尝试在指定枢纽把整程拆成 2 段，生成一个 SplitPlan；不可行返回 None。

    步骤（对应 SPEC §5.2）：
      1. 查两段（origin→hub、hub→dest）在 date 与 date+1 的 ECONOMY / BUSINESS；
      2. 用「90min <= 衔接 <= 20h」筛出可行的经济舱组合，取总价最低的一对做底价锚点；
      3. 公务舱（以及可选的超经）各取「价格最低且与该锚点另一段能衔接」的票段，
         写入 LegChoice.options；某个舱位完全不可行时就不写这个 key（上层显示「无报价」）；
      4. 任何一段完全没有 ECONOMY → 整个方案丢弃（经济舱是底价锚点）。

    单位：价格 CNY；时长与衔接 min；距离 km。
    """
    if q is None or fetch is None:
        return None
    origin = _code(q.origin)
    destination = _code(q.destination)
    hub_code = _code(hub)
    if not origin or not destination or not hub_code:
        return None
    if hub_code in (origin, destination):
        return None

    dates = [_add_days(q.date, offset) for offset in DATE_OFFSETS]
    cabins = list(REQUIRED_CABINS) + list(OPTIONAL_CABINS)

    # (leg_index, cabin) -> 候选票段列表
    pool: dict[tuple[int, str], list[Leg]] = {}
    for index, (start, end) in enumerate(((origin, hub_code), (hub_code, destination))):
        for cabin in cabins:
            parts: list[Leg] = []
            for day in dates:
                try:
                    got = fetch(start, end, day, cabin) or []
                except Exception:  # noqa: BLE001
                    if cabin in OPTIONAL_CABINS:
                        # 超经是可选舱位：provider 不支持时直接当作没有报价
                        got = []
                    else:
                        raise
                parts.extend(got)
            pool[(index, cabin)] = _clean_legs(_dedupe_legs(parts), q)

    # ---- 步骤 1/2：经济舱底价锚点 -------------------------------------------------
    eco_first = pool[(0, "ECONOMY")]
    eco_second = pool[(1, "ECONOMY")]
    if not eco_first or not eco_second:
        return None  # 某段没有经济舱 → 无法作为底价锚点，整案丢弃

    anchor: Optional[tuple[tuple, Leg, Leg, float]] = None
    for leg1 in eco_first:
        for leg2 in eco_second:
            gap = _connection_min(leg1, leg2)
            if gap is None:
                continue
            # 排序键：总价最低 → 总时长最短 → 起飞更早（结果确定、可复现）
            key = (
                round(float(leg1.price) + float(leg2.price), 2),
                float(leg1.duration_min) + float(leg2.duration_min) + gap,
                str(leg1.depart_local),
                str(leg2.depart_local),
            )
            if anchor is None or key < anchor[0]:
                anchor = (key, leg1, leg2, gap)
    if anchor is None:
        return None  # 有票但拼不出 90min~20h 的衔接
    _, eco_leg1, eco_leg2, layover_min = anchor

    # ---- 步骤 3：其它舱位（各自独立，绝不互相顶替） -------------------------------
    options_first: dict[str, Leg] = {"ECONOMY": eco_leg1}
    options_second: dict[str, Leg] = {"ECONOMY": eco_leg2}

    for cabin in OPTIONAL_CABINS + ("BUSINESS",):
        # 第一段升舱：必须能与第二段的「经济舱」衔接（best_value 只升一段时用它）
        candidates_first = [
            leg for leg in pool[(0, cabin)] if _connection_min(leg, eco_leg2) is not None
        ]
        if candidates_first:
            options_first[cabin] = min(candidates_first, key=_price_key)
        # 第二段升舱：必须能与第一段「经济舱」衔接；若第一段同舱位也有报价，
        # 优先选两边公务舱也能互相衔接的（这样「两段都升」的组合也不会时间错乱）
        candidates_second = [
            leg for leg in pool[(1, cabin)] if _connection_min(eco_leg1, leg) is not None
        ]
        if candidates_second:
            if cabin in options_first:
                mutual = [
                    leg
                    for leg in candidates_second
                    if _connection_min(options_first[cabin], leg) is not None
                ]
                if mutual:
                    candidates_second = mutual
            options_second[cabin] = min(candidates_second, key=_price_key)

    # ---- 步骤 4：组装 LegChoice / SplitPlan --------------------------------------
    distance_first = airports.distance_km(origin, hub_code)
    distance_second = airports.distance_km(hub_code, destination)
    legs = [
        LegChoice(
            index=0,
            origin=origin,
            destination=hub_code,
            distance_km=distance_first,
            tz_shift_h=airports.tz_shift_h(origin, hub_code),
            options=options_first,
        ),
        LegChoice(
            index=1,
            origin=hub_code,
            destination=destination,
            distance_km=distance_second,
            tz_shift_h=airports.tz_shift_h(hub_code, destination),
            options=options_second,
        ),
    ]
    label = (
        f"{airports.city_name(origin)} → {airports.city_name(hub_code)} + "
        f"{airports.city_name(hub_code)} → {airports.city_name(destination)}"
    )
    return SplitPlan(
        hub=hub_code,
        label=label,
        legs=legs,
        layovers_min=[layover_min],
        note=_build_note(hub_code, eco_leg1, eco_leg2),
    )


def build_plans(
    q: Query,
    fetch,
    hubs: Optional[list[str]] = None,
    progress: ProgressCallback = None,
) -> list[SplitPlan]:
    """对多个候选枢纽逐个 build_plan，返回可行的方案列表（保持枢纽顺序）。

    * `hubs is None`：用 `candidate_hubs(q)`（默认池已按绕行比排序并截断）；
    * 显式传 `hubs`：只做归一化/同城去重，不再做距离与绕行比过滤
      （用户明确指定了就尊重）。api.py 会把 candidate_hubs 的结果再传进来。
    * `progress(done, total, label)`：每处理完一个枢纽回调一次，label 是中文进度文案。

    单位：本函数不直接返回数字，方案内部的时长/价格单位见 SplitPlan 字段。
    """
    if q is None:
        return []
    if hubs is None:
        hub_list = candidate_hubs(q)
    else:
        hub_list = _sanitize_hub_list(q, hubs, limit=None)

    total = len(hub_list)
    plans: list[SplitPlan] = []
    _report_progress(progress, 0, total, "组装拆分方案")
    for index, hub in enumerate(hub_list, start=1):
        plan = build_plan(q, hub, fetch)
        if plan is not None:
            plans.append(plan)
        state = "可行" if plan is not None else "不可行"
        _report_progress(
            progress,
            index,
            total,
            f"组装拆分方案：{airports.city_name(hub)}（{state}，{index}/{total}）",
        )
    return plans

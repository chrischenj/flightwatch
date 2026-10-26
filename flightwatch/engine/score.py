"""休息价值评分（SPEC §5.3）。

纯本地计算，不联网、不依赖第三方库。

核心问题：一张票「能让人真正睡多久」不只取决于飞行时长，还取决于
  * 飞行时间是否落在夜间窗口内（出发地当地时间 21:00 ~ 次日 07:00）；
  * 起降前后各有一段无法入睡的时间（起飞后 30 分钟 / 落地前 45 分钟）；
  * 舱位带来的睡眠效率（经济舱 0.35，公务舱 1.00 …）。

单位约定（所有公开函数都在 docstring 里重复说明）：
  * 时间：函数名带 `_h` 的返回「小时 h」；内部时间窗计算用「分钟 min」；
  * 价格：CNY（元）；
  * rest_score：0~100 的无量纲分数。

算法与 SPEC §5.3 一一对应，公式里的每个常数都能在下面对应上：
    duration_h   = leg.duration_min / 60
    sleep_h      = 夜间窗口与可用睡眠窗口的交集（小时）
    night_ratio  = sleep_h / max(duration_h, 0.1)            # 0..1
    base_rest_h  = min(duration_h, sleep_h)                  # 真正能睡的小时
    comfort      = {ECONOMY:0.35, PREMIUM_ECONOMY:0.60, BUSINESS:1.00, FIRST_CLASS:1.15}
    effective_rest_h = base_rest_h * (0.15 + 0.85 * night_ratio) * comfort
    rest_score       = clamp(effective_rest_h / 8.0 * 100, 0, 100)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ..models import Leg, parse_dt

# 舱位「睡眠效率」系数。1.00 = 公务舱基线。
# 注意：这里刻意不复用 models.CABIN_COMFORT（那一套是 0~1.25 的「合意度」口径，
# 用于别的展示），因为 SPEC §5.3 明确给了这组数，混用会让分数不可复现。
COMFORT_FACTOR = {
    "ECONOMY": 0.35,
    "PREMIUM_ECONOMY": 0.60,
    "BUSINESS": 1.00,
    "FIRST_CLASS": 1.15,
}

# 夜间窗口：出发地当地时间 [21:00, 次日 07:00)
NIGHT_START_HOUR = 21
NIGHT_END_HOUR = 7

# 起飞后 30 分钟：滑行、爬升、发餐，基本睡不着（min）
PRE_DEPART_BUFFER_MIN = 30
# 落地前 45 分钟：下降准备、收小桌板，必须醒来（min）
PRE_LANDING_BUFFER_MIN = 45

# 8 小时有效休息记满分（SPEC §5.3 里除以 8.0 的来源）
FULL_SLEEP_H = 8.0

# 升舱「每多睡 1 小时成本」的分母下限（小时）。
# 设为 0.15 而不是 0：白天航段升舱带来的休息增益可能只有几分钟，
# 直接除会得到天文数字/除零，用 0.15h（9 分钟）作为「最小可感知收益」。
MIN_REST_DELTA_H = 0.15


# --------------------------------------------------------------------------- 内部工具
def _local_naive(value: str) -> Optional[datetime]:
    """把 ISO 时间串解析成「无时区的墙钟时间」。

    休息评分只关心出发地当地的挂钟时间（SPEC §5.3「按出发地当地时间」），
    所以即使字符串里带 +08:00 之类的偏移，也先剥掉时区再算，
    避免把不同城市的偏移混进同一根时间轴。
    """
    if not value:
        return None
    try:
        dt = parse_dt(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _duration_h(leg: Leg) -> float:
    """飞行时长（小时，h）。非法值按 0 处理。"""
    try:
        return max(float(leg.duration_min or 0.0), 0.0) / 60.0
    except (TypeError, ValueError):
        return 0.0


def _sleep_window(leg: Leg) -> Optional[tuple[datetime, datetime]]:
    """可睡眠窗口：起飞后 30 分钟 ~ 落地前 45 分钟（出发地当地时间）。

    注意：落地时刻用 `depart_local + duration_min` 换算到「出发地时间轴」，
    而不是直接用 leg.arrive_local —— 后者是到达城市当地时间，两个城市有时差，
    直接相减会把时差算进去。duration_min 才是真实经过的分钟数。
    航段太短（<= 30+45 分钟）时返回 None。
    """
    dep = _local_naive(getattr(leg, "depart_local", "") or "")
    if dep is None:
        return None
    try:
        duration_min = float(leg.duration_min or 0.0)
    except (TypeError, ValueError):
        return None
    if duration_min <= PRE_DEPART_BUFFER_MIN + PRE_LANDING_BUFFER_MIN:
        return None
    start = dep + timedelta(minutes=PRE_DEPART_BUFFER_MIN)
    end = dep + timedelta(minutes=duration_min - PRE_LANDING_BUFFER_MIN)
    if end <= start:
        return None
    return start, end


def _night_overlap_h(start: datetime, end: datetime) -> float:
    """[start, end) 与「每晚 21:00~次日 07:00」的交集总小时数（h）。

    跨夜航班会跨多个夜晚，所以逐天枚举夜间窗口再求交集。
    """
    if end <= start:
        return 0.0
    total_min = 0.0
    # 从前一天 0 点开始枚举，保证覆盖「start 当天凌晨属于前一晚」的情况
    day = (start - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    guard = 0
    while day <= end and guard < 100:  # guard 只是防呆，正常航班最多跨 2~3 晚
        night_start = day + timedelta(hours=NIGHT_START_HOUR)
        night_end = day + timedelta(hours=24 + NIGHT_END_HOUR)
        lo = max(start, night_start)
        hi = min(end, night_end)
        if hi > lo:
            total_min += (hi - lo).total_seconds() / 60.0
        day += timedelta(days=1)
        guard += 1
    return total_min / 60.0


# --------------------------------------------------------------------------- 公开接口
def sleep_window_h(leg: Leg) -> float:
    """该段落在夜间窗口 [21:00, 07:00) 内、扣除起降缓冲后可用于睡眠的小时数（h）。

    api.py 直接用这个数字展示「本段能睡几小时」，与 rest_score 内部完全同源。
    """
    if leg is None:
        return 0.0
    window = _sleep_window(leg)
    if window is None:
        return 0.0
    return _night_overlap_h(*window)


def night_ratio(leg: Leg) -> float:
    """夜间睡眠占比 = sleep_window_h / 飞行小时数，范围 0..1。

    1.0 表示整段飞行都落在夜间可睡窗口里；白天短途通常接近 0。
    """
    if leg is None:
        return 0.0
    duration_h = _duration_h(leg)
    ratio = sleep_window_h(leg) / max(duration_h, 0.1)
    return max(0.0, min(1.0, ratio))


def effective_rest_h(leg: Leg, cabin: Optional[str] = None) -> float:
    """有效休息小时数（h）：真正「睡回来」的小时，已经过夜间占比和舱位效率折算。

    参数:
        leg:   票段（用 depart_local / duration_min 计算，不看到达城市时区）。
        cabin: 舱位；None 时用 `leg.cabin`（SPEC §4 约定）。

    公式（SPEC §5.3）:
        base_rest_h * (0.15 + 0.85 * night_ratio) * comfort[cabin]
    其中 0.15 是「白天也能休息一点」的底子，0.85 是夜间时间的额外权重。
    """
    if leg is None:
        return 0.0
    cabin = cabin or getattr(leg, "cabin", "ECONOMY") or "ECONOMY"
    duration_h = _duration_h(leg)
    sleep_h = sleep_window_h(leg)
    ratio = night_ratio(leg)
    # min(duration_h, sleep_h)：sleep_h 理论上已经小于 duration_h，写 min 是照公式防呆
    base_rest_h = min(duration_h, sleep_h)
    comfort = COMFORT_FACTOR.get(cabin, COMFORT_FACTOR["ECONOMY"])
    return base_rest_h * (0.15 + 0.85 * ratio) * comfort


def rest_score(leg: Leg, cabin: Optional[str] = None) -> float:
    """休息价值分数，0~100（分）。8 小时有效休息 = 100 分，超出上限截断。"""
    score = effective_rest_h(leg, cabin) / FULL_SLEEP_H * 100.0
    return max(0.0, min(100.0, score))


def cost_per_rest_hour(eco: Leg, biz: Leg) -> Optional[float]:
    """升舱「每多换 1 小时有效休息要多花多少钱」，单位 CNY/小时（元/小时）。

    即 SPEC §5.3 的 (biz.price - eco.price) / max(rest_biz - rest_eco, 0.15)。
    分母用 0.15h 兜底（见 MIN_REST_DELTA_H），避免白天航段休息增益≈0 时爆炸。
    只要有一段没有报价（None）就返回 None，表示无法评估。
    """
    if eco is None or biz is None:
        return None
    try:
        extra_price = float(biz.price) - float(eco.price)
    except (TypeError, ValueError):
        return None
    extra_rest = effective_rest_h(biz) - effective_rest_h(eco)
    return extra_price / max(extra_rest, MIN_REST_DELTA_H)


def rest_detail(leg: Leg, cabins: Optional[list[str]] = None) -> dict:
    """给 api.py 组装 `legs[].rest` 用的汇总块（SPEC §6）。

    返回:
        {"score": 0..100 分, "effective_rest_h": {舱位: 小时},
         "sleep_h": 小时, "night_ratio": 0..1, "verdict": 中文短句}
    该函数只是把上面几个公开函数包一层，数字与评分完全一致；
    api.py 若想自己拼也可以，不强制使用。
    """
    if leg is None:
        return {
            "score": 0.0,
            "effective_rest_h": {},
            "sleep_h": 0.0,
            "night_ratio": 0.0,
            "verdict": "该段暂无报价",
        }
    if cabins is None:
        cabins = ["ECONOMY", "BUSINESS"]
        own = getattr(leg, "cabin", None)
        if own and own not in cabins:
            cabins.append(own)
    effective = {c: round(effective_rest_h(leg, c), 2) for c in cabins}
    score = round(rest_score(leg), 1)
    if score >= 55:
        verdict = "夜间长途，升舱的休息收益最大"
    elif score >= 30:
        verdict = "有可用的跨夜睡眠窗口，可按预算考虑升舱"
    elif score >= 10:
        verdict = "休息价值有限，只为睡觉升舱不划算"
    else:
        verdict = "以白天航段为主，升舱基本换不到睡眠"
    return {
        "score": score,
        "effective_rest_h": effective,
        "sleep_h": round(sleep_window_h(leg), 2),
        "night_ratio": round(night_ratio(leg), 2),
        "verdict": verdict,
    }

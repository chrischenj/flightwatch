"""组合枚举、帕累托前沿与推荐结论（SPEC §5.4 / §5.5）。

纯计算，不联网。输入是为数不多的 SplitPlan，输出可直接 JSON 序列化的 dict。

单位约定：
  * 价格：CNY（元）；`price_delta` / `extra_cost` 也是元；
  * 时长：分钟 min（`total_duration_min` / `total_ground_min`）；
  * 有效休息：小时 h（`comfort_score` = Σ effective_rest_h，`rest_gain` / `extra_rest_h`）；
  * `value_per_rest_hour` / `cost_per_rest_h`：元/小时（CNY per hour）。

「组合」= 每段各选一个舱位。所有数字都相对「本方案全经济舱」这个底价锚点
（经济舱是整程底价，也是平台整程票最直接的对照物）。
"""
from __future__ import annotations

from itertools import product
from typing import Optional

from .. import airports
from ..models import CABIN_CN, CABINS, Combo, SplitPlan, minutes_between
from .score import MIN_REST_DELTA_H, effective_rest_h

# 衔接时间偏紧提示阈值（min）：低于 2 小时会给用户「可能误机」的风险提示。
TIGHT_CONNECT_MIN = 120

# 超长中转阈值（min）：超过 8 小时说明这个组合等的是另一班飞机，
# 升舱的差价里其实包含了「换到更晚/更早班次」的代价，要在文案里点明。
LONG_GROUND_MIN = 8 * 60

# 升舱允许带来的额外全程时长上限（min）：4 小时。
# 为什么是 4h：cost_per_rest_hour 只衡量「钱 / 休息」，但升舱常常同时换到了更晚的
# 班次。若全程因此多出 4 小时以上，多睡的那点休息已经被路上干等和疲劳抵消，
# 用户不会这么飞，所以这类组合不做「最值得升舱」候选、也不进推荐。
MAX_EXTRA_DURATION_MIN = 240

# 升舱的「绝对值」门槛（比「在候选里相对最优」更重要）：
# * 多睡不到 1 小时，为换票/改签/行李重新托运折腾一圈不值得；
# * 每多睡 1 小时花费超过 4000 元，已远超「值得升舱」的心理价位，应直接劝退。
# live 数据里出现过「多花 2 万、多睡 0.6h、约 3.7 万元/小时」的候选，
# 若只按相对排序仍会被包装成推荐，所以必须用绝对值判据拦住。
MIN_EXTRA_REST_H = 1.0
MAX_COST_PER_REST_H = 4000.0

# SPEC §5.5 要求必须出现的风险提示（这些是分段票的结构性风险，不随方案变化）。
RISK_WARNINGS = [
    "分段票不是联程票：前一段延误/取消，后一段不负责改签，误机一般按自愿弃程处理。",
    "行李通常不能直挂：跨航司中转需取出行李重新托运并重新安检，务必留足中转时间。",
    "请核对中转国/地区的过境签证要求（美国、申根、英国、加拿大尤其严格）。",
    "价格为公开接口实时枚举的最低档位，可能存在缓存延迟，下单前必须在航司或 OTA 页面复核。",
]


# --------------------------------------------------------------------------- 内部工具
def _baseline_combo(combos: list[Combo]) -> Optional[Combo]:
    """本方案的「全经济舱」基准组合；数据异常没有全经济舱时退化为最便宜的组合。"""
    for combo in combos:
        if all(cabin == "ECONOMY" for cabin in combo.cabins):
            return combo
    return min(combos, key=lambda c: (c.total_price, c.total_duration_min)) if combos else None


def _all_cabin_combo(combos: list[Combo], cabin: str) -> Optional[Combo]:
    """找到「每段都是指定舱位」的组合（比如两段全公务舱）；不存在返回 None。"""
    for combo in combos:
        if all(item == cabin for item in combo.cabins):
            return combo
    return None


def _fmt_money(value: float, currency: str = "CNY") -> str:
    """金额格式化：CNY 显示「1,234 元」，其它币种显示「1,234 USD」。"""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"
    code = (currency or "CNY").upper()
    if code == "CNY":
        return f"{amount:,.0f} 元"
    return f"{amount:,.0f} {code}"


def _baseline_price(baseline: Optional[dict], key: str) -> Optional[float]:
    """从 api.py 的 baseline 结构里安全取价格（可能是 None / 缺字段 / 字符串）。"""
    if not isinstance(baseline, dict):
        return None
    node = baseline.get(key)
    value = node.get("price") if isinstance(node, dict) else node
    if value is None:
        value = baseline.get(f"{key}_price")
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _combo_currency(combos: list[Combo]) -> str:
    """取组合使用的币种（同一方案各段一般一致）。"""
    for combo in combos:
        if combo.currency:
            return combo.currency
    return "CNY"


def _route_label(plan: SplitPlan, leg_index: int) -> str:
    """第 N 段的「出发城市→到达城市」中文标签。"""
    try:
        choice = plan.legs[leg_index]
    except (IndexError, AttributeError):
        return f"第 {leg_index + 1} 段"
    return f"{airports.city_name(choice.origin)}→{airports.city_name(choice.destination)}"


def _ground_min_for(legs: list) -> float:
    """按「本组合实际选中的票段」逐对计算地面衔接时长（min）。

    关键点：相邻两段的落地/起飞都发生在**同一个枢纽**，用的是该枢纽当地时间，
    所以墙钟直接相减就是中转等待分钟数，不需要再做时区修正。
    与 plan.layovers_min 的区别：那个是「经济舱锚点」的衔接，不同舱位选到的
    往往不是同一班飞机（价格最低枚举所致），每个组合必须算自己的 ground。
    时间串解析失败的那一对按 0 处理（不让脏数据把整条推荐链打断）。
    """
    total = 0.0
    for previous, following in zip(legs, legs[1:]):
        try:
            gap = minutes_between(previous.arrive_local, following.depart_local)
        except (ValueError, TypeError):
            continue
        total += max(0.0, float(gap))
    return total


def _transfer_note(ground_min: float, cabins: list[str]) -> str:
    """超长中转说明文案：提醒用户「这次升舱其实换到了另一班飞机」。

    返回空串表示不需要提示；units: ground_min 分钟。
    """
    if ground_min <= LONG_GROUND_MIN:
        return ""
    hours = ground_min / 60.0
    if any(cabin != "ECONOMY" for cabin in cabins):
        return (
            f"本次升舱同时换到了更晚的班次：中转等待 {hours:.1f} 小时"
            f"（{ground_min:.0f} 分钟）。"
        )
    return f"该组合的中转等待 {hours:.1f} 小时（{ground_min:.0f} 分钟）。"


def _duration_guard(combo: Combo, baseline: Optional[Combo]) -> tuple[float, bool]:
    """组合相对「本方案全经济舱」的额外全程时长（min）以及是否超过护栏。

    `duration_delta_min` / `too_long` 由 build_combos 挂在 Combo 实例属性上
    （models.Combo 是 Lead 冻结的模型，不能加字段，Combo.to_dict() 也不会多字段）；
    这里做防御性读取，拿不到时按 baseline 现算。
    返回: (额外时长分钟, 是否 too_long)，正数表示比全经济舱更久。
    """
    delta = getattr(combo, "duration_delta_min", None)
    too_long = getattr(combo, "too_long", None)
    if delta is None or too_long is None:
        if baseline is None:
            return 0.0, False
        delta = combo.total_duration_min - baseline.total_duration_min
        too_long = delta > MAX_EXTRA_DURATION_MIN
    return float(delta), bool(too_long)


def _qualifies(combo: Combo, baseline: Optional[Combo]) -> bool:
    """升舱是否达到「值得推荐」的绝对门槛（不只看候选里谁相对最优）。

    必须同时满足：
      1. 全程时长未超护栏（duration_delta <= MAX_EXTRA_DURATION_MIN）；
      2. 多睡 >= MIN_EXTRA_REST_H（1 小时）——多睡半小时不值得为换票/行李折腾；
      3. value_per_rest_hour 不为 None 且 <= MAX_COST_PER_REST_H（4000 元/小时）。

    live 数据里出现过 rest_gain <= 0（升了反而更没得睡，例如 -0.5h）的组合，
    这类一律返回 False，不参与 best_value / recommend。
    """
    if combo is None or baseline is None:
        return False
    _duration_delta, too_long = _duration_guard(combo, baseline)
    if too_long:
        return False
    rest_gain = combo.comfort_score - baseline.comfort_score
    if rest_gain < MIN_EXTRA_REST_H:
        return False
    cost = combo.value_per_rest_hour
    if cost is None:
        # 兜底：build_combos 在 rest_gain > 0 时已算好；这里防止外部手工构造的 Combo 缺字段
        cost = (combo.total_price - baseline.total_price) / max(rest_gain, MIN_REST_DELTA_H)
    return cost <= MAX_COST_PER_REST_H


def _candidate_dict(record: dict) -> dict:
    """把候选记录转成给 api/前端用的 dict（字段与 best_value 对齐）。"""
    combo = record["combo"]
    cost = record.get("cost_per_rest")
    return {
        "plan_hub": record["plan"].hub,
        "leg_index": record["leg_index"],
        "cabin": record["cabin"],
        "extra_cost": round(record["extra_cost"], 2),
        "extra_rest_h": round(record["extra_rest"], 2),
        "cost_per_rest_h": round(cost, 2) if cost is not None else None,
        "transfer_note": _transfer_note(combo.total_ground_min, combo.cabins),
    }


def _record_cost_key(record: dict) -> float:
    """候选按单位休息成本（元/小时）排序的键；没有有效 cpr 的排最后。"""
    cost = record.get("cost_per_rest")
    return float("inf") if cost is None else float(cost)


def _excluded_reason(record: dict) -> str:
    """说明某个原本性价比最高的升舱候选为何被「时长护栏」排除（带具体数字）。"""
    plan = record["plan"]
    combo = record["combo"]
    duration_h = record["duration_delta"] / 60.0
    ground_h = combo.total_ground_min / 60.0
    cabin_cn = CABIN_CN.get(record["cabin"], record["cabin"])
    return (
        f"{airports.city_name(plan.hub)}方案的「第{record['leg_index'] + 1}段升{cabin_cn}」"
        f"会让全程多 {duration_h:.1f} 小时（中转等待 {ground_h:.1f} 小时），"
        f"超过 {MAX_EXTRA_DURATION_MIN} 分钟时长护栏，已排除。"
    )


def _layover_text(plan: SplitPlan, combo: Combo) -> str:
    """衔接说明：优先用这个组合自己的地面时间；算不出来时才退回方案级锚点。"""
    if combo.total_ground_min > 0:
        return f"在{airports.city_name(plan.hub)}中转，衔接 {combo.total_ground_min:.0f} 分钟"
    if plan.layovers_min:
        lay_min = min(plan.layovers_min)
        return f"在{airports.city_name(plan.hub)}中转，最短衔接 {lay_min:.0f} 分钟"
    return f"在{airports.city_name(plan.hub)}中转，地面时间未提供"


# --------------------------------------------------------------------------- 公开接口
def build_combos(plan: SplitPlan) -> list[Combo]:
    """枚举一个方案的所有舱位组合，并附带相对全经济舱的差价/休息增益（SPEC §5.4）。

    * 每段只枚举 `options` 里真实存在的舱位（缺失舱位不占位、不顶替）；
    * 总价 = Σ段票价（CNY）；
    * 总时长 = Σ飞行分钟 + **本组合自己的**地面衔接分钟（min）。
      注意：不同舱位选到的往往不是同一班飞机，所以每个组合的 ground 各自计算
      （plan.layovers_min 只是经济舱锚点的衔接，不能套给所有组合）；
    * comfort_score = Σ effective_rest_h（小时）；
    * price_delta / rest_gain / value_per_rest_hour 均相对「本方案全经济舱」计算；
    * 时长护栏：每个组合还会得到 duration_delta_min（相对全经济舱多出的全程分钟数）
      与 too_long（是否超过 MAX_EXTRA_DURATION_MIN=240min）。这两个值只挂在实例属性上、
      由 annotate() 输出成 dict 附加键，不写进 models.Combo（Lead 冻结模型）。

    返回顺序固定为 itertools.product(CABINS 顺序)，所以两段方案的第一条
    永远是「经济舱+经济舱」，方便 UI 取基准／按索引画帕累托点。
    """
    if plan is None or not getattr(plan, "legs", None):
        return []

    available: list[list[str]] = []
    for choice in plan.legs:
        options = getattr(choice, "options", None) or {}
        cabins = [cabin for cabin in CABINS if options.get(cabin) is not None]
        if not cabins:
            return []  # 有一段没有任何报价，组合无意义
        available.append(cabins)

    combos: list[Combo] = []
    for cabin_tuple in product(*available):
        chosen_legs = [plan.legs[i].options[cabin] for i, cabin in enumerate(cabin_tuple)]
        total_price = sum(float(leg.price) for leg in chosen_legs)
        # 各组自己的地面衔接：升舱往往意味着换了另一班飞机，等待时长不同
        ground_min = _ground_min_for(chosen_legs)
        total_duration = sum(float(leg.duration_min or 0.0) for leg in chosen_legs) + ground_min
        comfort = sum(
            effective_rest_h(leg, cabin) for leg, cabin in zip(chosen_legs, cabin_tuple)
        )
        currency = chosen_legs[0].currency or "CNY"
        combos.append(
            Combo(
                plan_hub=plan.hub,
                cabins=list(cabin_tuple),
                total_price=total_price,
                currency=currency,
                total_duration_min=total_duration,
                total_ground_min=ground_min,
                comfort_score=comfort,
                legs=list(chosen_legs),
            )
        )

    if not combos:
        return []

    baseline = _baseline_combo(combos)
    for combo in combos:
        combo.price_delta = combo.total_price - baseline.total_price
        combo.rest_gain = combo.comfort_score - baseline.comfort_score
        if combo.rest_gain > 1e-9:
            # 元/小时：本组合相对全经济舱，每多换 1 小时有效休息要多花多少钱
            combo.value_per_rest_hour = combo.price_delta / max(
                combo.rest_gain, MIN_REST_DELTA_H
            )
        else:
            combo.value_per_rest_hour = None
        # 时长护栏元数据（相对全经济舱）：不写进 models.Combo 的字段，
        # 只挂实例属性，annotate() 再输出成 dict 的附加键 duration_delta_min / too_long。
        combo.duration_delta_min = combo.total_duration_min - baseline.total_duration_min
        combo.too_long = combo.duration_delta_min > MAX_EXTRA_DURATION_MIN
    return combos


def _best_value(combos: list[Combo]) -> Optional[dict]:
    """「只升一段」里 cost_per_rest_hour 最小的那个（SPEC §5.4）。

    只考虑相对全经济舱**恰好改了一段**的组合；每多睡 1 小时的代价用
    extra_cost / max(extra_rest, 0.15) 计算（分母兜底见 score.MIN_REST_DELTA_H）。
    只有通过 `_qualifies` 绝对门槛（不超时长护栏 / 多睡>=1h / <=4000 元每小时）的
    组合才是候选；都不合格时返回 None（不再包装出一个「相对最优」的推荐）。
    返回单位：extra_cost 元、extra_rest_h 小时、cost_per_rest_h 元/小时。
    """
    if not combos:
        return None
    baseline = _baseline_combo(combos)
    if baseline is None:
        return None

    best: Optional[tuple[tuple, dict]] = None
    for combo in combos:
        changed = [(i, cabin) for i, cabin in enumerate(combo.cabins) if cabin != "ECONOMY"]
        if len(changed) != 1:
            continue  # 不是「只升一段」
        if not _qualifies(combo, baseline):
            continue  # 未过绝对门槛，不做「最值得升舱」候选
        leg_index, cabin = changed[0]
        extra_cost = combo.total_price - baseline.total_price
        extra_rest = combo.comfort_score - baseline.comfort_score
        cost_per_rest = extra_cost / max(extra_rest, MIN_REST_DELTA_H)
        # 代价最低优先；同价时优先休息增益更大、段序号更靠前的
        key = (round(cost_per_rest, 6), -round(extra_rest, 6), leg_index)
        if best is None or key < best[0]:
            best = (
                key,
                {
                    "leg_index": leg_index,
                    "cabin": cabin,
                    "extra_cost": round(extra_cost, 2),
                    "extra_rest_h": round(extra_rest, 2),
                    "cost_per_rest_h": round(cost_per_rest, 2),
                    # 超过 8 小时的中转要说明「升舱同时换了班次」
                    "transfer_note": _transfer_note(
                        combo.total_ground_min, combo.cabins
                    ),
                },
            )
    return best[1] if best else None


def _pareto_indices(combos: list[Combo]) -> list[int]:
    """非支配集合的索引（总价升序、rest_gain 降序），SPEC §5.4。

    支配定义：A 支配 B 当且仅当 A 总价 <= B 总价 且 A 的 rest_gain >= B 的 rest_gain，
    且至少有一个严格不等。返回的索引可直接给前端画帕累托前沿（价格越左越好、
    休息越多越好）。
    """
    if not combos:
        return []
    order = sorted(
        range(len(combos)), key=lambda i: (combos[i].total_price, -combos[i].rest_gain)
    )
    pareto: list[int] = []
    best_rest = float("-inf")
    for index in order:
        rest_gain = combos[index].rest_gain
        # 价格不降的前提下，只有休息增益严格超过此前所有点，才是新的非支配点
        if rest_gain > best_rest + 1e-9:
            pareto.append(index)
            best_rest = rest_gain
    return pareto


def annotate(plan: SplitPlan) -> dict:
    """给一个方案做完整标注（SPEC §4）。

    返回的 dict 可以直接塞进 api.py 的 JSON（内部已把 Combo 转成 dict）：
        {"combos": [Combo.to_dict() + {"transfer_note": 超长中转说明,
                                      "duration_delta_min": 相对全经济舱多出的分钟,
                                      "too_long": 是否超过 240 分钟护栏}, ...],
         "best_value": {...} | None,
         "pareto": [组合下标, ...]}
    `pareto` 的下标指向 `combos` 列表顺序；`best_value` 见 `_best_value`。
    注意：不改 models.Combo 的字段，以上附加键只出现在输出 dict 上，
    api.py 的 Combo.to_dict() 序列化不受影响。
    """
    combos = build_combos(plan)
    baseline = _baseline_combo(combos)
    annotated: list[dict] = []
    for combo in combos:
        item = combo.to_dict()
        # 超长中转提示：让用户知道差价买的不只是舱位，还换了班次
        item["transfer_note"] = _transfer_note(combo.total_ground_min, combo.cabins)
        # 时长护栏（相对全经济舱）：给前端/推荐用的展示字段
        duration_delta, too_long = _duration_guard(combo, baseline)
        item["duration_delta_min"] = round(duration_delta, 1)
        item["too_long"] = too_long
        annotated.append(item)
    return {
        "combos": annotated,
        "best_value": _best_value(combos),
        "pareto": _pareto_indices(combos),
    }


def recommend(plans: list[SplitPlan], baseline: Optional[dict] = None) -> dict:
    """给全部方案一个推荐结论（SPEC §5.4 / §5.5）。

    参数:
        plans:    SplitPlan 列表（api.py 传入；可能为空）。
        baseline: api.py 的整程直搜对照价，结构见 SPEC §6：
                  {"through_economy": {"price": ...} | None,
                   "through_business": {"price": ...} | None}
                  只用于「比整程票便宜/贵多少」的数字对比，可为 None。

    返回:
        {"headline": 一句话结论（含数字）,
         "reasons": 最多 4 条、每条都带数字的理由,
         "combo": 推荐组合的 Combo.to_dict()（无方案时为 None）,
         "upgrade_if_forced": "如果一定要升，相对最划算的一段"（dict 或 None，不写进 headline）,
         "warnings": §5.5 强制的 4 条风险提示 + 动态风险}

    策略：
      1. 候选必须通过 `_qualifies` 绝对门槛：不超时长护栏、多睡 >= 1 小时、
         每多睡 1 小时 <= 4000 元；在合格候选里选单位休息成本最低的推荐；
      2. 没有任何候选合格时：headline 明确写「本轮不建议升舱」，reasons 给出
         最有参考价值的数字（最便宜的升舱也要 X 元/小时、多睡 Y 小时），
         同时 `upgrade_if_forced` 仍给出「一定要升时相对最划算」的那段（超时长的除外）；
      3. 所有升舱组合都超时长护栏时 `upgrade_if_forced = None`；
      4. 所有理由都必须能落到具体数字上，避免空泛建议。
    """
    plan_list = [p for p in (plans or []) if p is not None and getattr(p, "legs", None)]
    if not plan_list:
        return {
            "headline": "没有找到可行的分舱段方案：建议改用整程票，或换日期/枢纽再试。",
            "reasons": [],
            "combo": None,
            "upgrade_if_forced": None,
            "warnings": list(RISK_WARNINGS),
        }

    # 每个方案先算好组合与基线（组合计算很轻，重复调用可接受）
    entries: list[tuple[SplitPlan, list[Combo], Combo]] = []
    for plan in plan_list:
        combos = build_combos(plan)
        if not combos:
            continue
        base = _baseline_combo(combos)
        if base is None:
            continue
        entries.append((plan, combos, base))
    if not entries:
        return {
            "headline": "找到了拆分枢纽，但都没有可组合的报价：建议换日期或换枢纽再试。",
            "reasons": [],
            "combo": None,
            "upgrade_if_forced": None,
            "warnings": list(RISK_WARNINGS),
        }

    currency = _combo_currency(entries[0][1])
    through_eco = _baseline_price(baseline, "through_economy")
    through_biz = _baseline_price(baseline, "through_business")

    # ---- 1) 扫描「只升一段」候选：合格 / 超时长 / 贵或睡太少 / 升了反而更少 -------
    best_upgrade: Optional[tuple] = None  # 合格候选里 cpr 最低的
    excluded: list[dict] = []             # 超时长护栏（>4h）的候选
    reference: list[dict] = []            # 未超时长但绝对值不合格（贵 / 多睡不足 1h）
    for plan, combos, base in entries:
        for combo in combos:
            changed = [
                (i, cabin) for i, cabin in enumerate(combo.cabins) if cabin != "ECONOMY"
            ]
            if len(changed) != 1:
                continue
            leg_index, cabin = changed[0]
            extra_rest = combo.comfort_score - base.comfort_score
            extra_cost = combo.total_price - base.total_price
            duration_delta, too_long = _duration_guard(combo, base)
            cost_per_rest = (
                extra_cost / max(extra_rest, MIN_REST_DELTA_H) if extra_rest > 0 else None
            )
            record = {
                "plan": plan,
                "combos": combos,
                "base": base,
                "combo": combo,
                "leg_index": leg_index,
                "cabin": cabin,
                "extra_cost": extra_cost,
                "extra_rest": extra_rest,
                "cost_per_rest": cost_per_rest,
                "duration_delta": duration_delta,
                "too_long": too_long,
            }
            if _qualifies(combo, base):
                # 通过绝对门槛：时长、多睡小时数、每小时成本都达标 → 可以推荐
                key = (round(cost_per_rest, 6), -round(extra_rest, 6), combo.total_price)
                if best_upgrade is None or key < best_upgrade[0]:
                    best_upgrade = (key, record)
            elif too_long:
                # 时长护栏：钱和休息之外，时间也是代价（多等 4h 以上不推荐）
                excluded.append(record)
            elif extra_rest > 0:
                # 未超时长，但「多睡太少」或「每多睡 1 小时太贵」→ 只作参考/劝退理由
                reference.append(record)
            # extra_rest <= 0（升了反而更没得睡，例如 -0.5h）：不参与任何推荐

    if best_upgrade is not None:
        record = best_upgrade[1]
        plan = record["plan"]
        combos = record["combos"]
        base = record["base"]
        chosen = record["combo"]
        leg_index = record["leg_index"]
        cabin = record["cabin"]
        extra_cost = record["extra_cost"]
        extra_rest = record["extra_rest"]
        cost_per_rest = record["cost_per_rest"]
        duration_delta = record["duration_delta"]
        cabin_cn = CABIN_CN.get(cabin, cabin)
        hub_cn = airports.city_name(plan.hub)
        headline = (
            f"在{hub_cn}中转时，把第 {leg_index + 1} 段升为{cabin_cn}最划算："
            f"多花 {extra_cost:,.0f} 元换 {extra_rest:.1f} 小时有效休息，"
            f"约 {cost_per_rest:,.0f} 元/小时。"
        )
        reasons: list[str] = []
        # ① 拆分 vs 整程经济舱的价格对比
        if through_eco is not None:
            diff = base.total_price - through_eco
            if diff <= 0:
                reasons.append(
                    f"拆分后全经济舱 {_fmt_money(base.total_price, currency)}，"
                    f"比整程经济舱 {_fmt_money(through_eco, currency)}便宜 "
                    f"{_fmt_money(abs(diff), currency)}。"
                )
            else:
                reasons.append(
                    f"拆分后全经济舱 {_fmt_money(base.total_price, currency)}，"
                    f"比整程经济舱 {_fmt_money(through_eco, currency)}贵 "
                    f"{_fmt_money(diff, currency)}（拆票不省钱，买的是休息与灵活性）。"
                )
        else:
            reasons.append(
                f"拆分后全经济舱底价 {_fmt_money(base.total_price, currency)}，"
                f"共 {len(plan.legs)} 段，可在{hub_cn}自由选择升舱段落。"
            )
        # ② 升舱性价比（数字是核心卖点；同时说明全程时长代价）
        if duration_delta <= 1:
            duration_text = "全程时长基本不变"
        elif duration_delta < 0:
            duration_text = f"全程反而少 {-duration_delta:.0f} 分钟"
        else:
            duration_text = f"全程多 {duration_delta:.0f} 分钟"
        reasons.append(
            f"第 {leg_index + 1} 段（{_route_label(plan, leg_index)}）升{cabin_cn}："
            f"多花 {_fmt_money(extra_cost, currency)}，多换 {extra_rest:.1f} 小时有效休息，"
            f"约 {cost_per_rest:,.0f} 元/小时；{duration_text}"
            f"（未超过 {MAX_EXTRA_DURATION_MIN} 分钟时长护栏）。"
        )
        # ③ 若有更高性价比的升舱因「全程多 4h 以上」被排除，明确说明原因；
        #    没有排除项时才补充「两段都升公务舱」的对比（理由 ≤4 条，二选一）
        if excluded:
            worst = min(excluded, key=_record_cost_key)
            reasons.append(_excluded_reason(worst))
        else:
            biz_combo = _all_cabin_combo(combos, "BUSINESS")
            if biz_combo is not None:
                if through_biz is not None:
                    saved = through_biz - biz_combo.total_price
                    if saved >= 0:
                        reasons.append(
                            f"若两段都升公务舱，总价 {_fmt_money(biz_combo.total_price, currency)}，"
                            f"比整程公务舱 {_fmt_money(through_biz, currency)}省 "
                            f"{_fmt_money(saved, currency)}。"
                        )
                    else:
                        reasons.append(
                            f"若两段都升公务舱，总价 {_fmt_money(biz_combo.total_price, currency)}，"
                            f"比整程公务舱 {_fmt_money(through_biz, currency)}贵 "
                            f"{_fmt_money(abs(saved), currency)}。"
                        )
                else:
                    reasons.append(
                        f"若两段都升公务舱，总价 {_fmt_money(biz_combo.total_price, currency)}，"
                        f"比全经济舱多花 {_fmt_money(biz_combo.price_delta, currency)}、"
                        f"多换 {biz_combo.rest_gain:.1f} 小时有效休息。"
                    )
        # ④ 衔接与总时长（把「代价」也量化出来；超长中转要说明换了班次）
        transfer_note = _transfer_note(chosen.total_ground_min, chosen.cabins)
        layover_reason = (
            f"{_layover_text(plan, chosen)}（≥90 分钟要求），"
            f"全程含中转约 {chosen.total_duration_min / 60.0:.1f} 小时。"
        )
        if transfer_note:
            layover_reason += transfer_note
        reasons.append(layover_reason)
        warnings = _warning_list(plan, base, chosen, through_eco, currency)
        combo_dict = chosen.to_dict()
        combo_dict["transfer_note"] = transfer_note
        return {
            "headline": headline,
            "reasons": reasons[:4],
            "combo": combo_dict,
            # 合格时「一定要升」的答案就是当前推荐的这个组合
            "upgrade_if_forced": _candidate_dict(record),
            "warnings": warnings,
        }

    # ---- 2) 没有任何「合格」的升舱候选 → 明确不建议升舱，但保留「强制升舱」参考 -----
    plan, combos, base = min(
        entries, key=lambda item: (item[2].total_price, item[2].total_duration_min, item[0].hub)
    )
    hub_cn = airports.city_name(plan.hub)
    reasons: list[str] = []
    upgrade_if_forced: Optional[dict] = None

    if reference:
        # 相对最划算、但没过绝对门槛的候选：只作参考，并给出劝退数字
        forced = min(reference, key=_record_cost_key)
        upgrade_if_forced = _candidate_dict(forced)
        forced_rest = forced["extra_rest"]
        forced_cost = forced["cost_per_rest"]
        if forced_cost is not None and forced_cost > MAX_COST_PER_REST_H:
            headline = (
                f"本轮不建议升舱：相对最划算的一段升舱也要约 {forced_cost:,.0f} 元/小时，"
                f"超过 {MAX_COST_PER_REST_H:,.0f} 元/小时的心理价位。"
            )
            reasons.append(
                f"最便宜的一段升舱也要 {_fmt_money(forced_cost, currency)}/小时"
                f"（多睡 {forced_rest:.1f} 小时），超出 {MAX_COST_PER_REST_H:,.0f} 元/小时的心理价位；"
                "这段钱花在别处更划算。"
            )
        else:
            headline = (
                f"本轮不建议升舱：相对最划算的一段也只多睡 {forced_rest:.1f} 小时，"
                f"不到 {MIN_EXTRA_REST_H:g} 小时的门槛。"
            )
            reasons.append(
                f"最多的一段升舱也只多睡 {forced_rest:.1f} 小时"
                f"（约 {_fmt_money(forced_cost or 0.0, currency)}/小时），"
                f"不到 {MIN_EXTRA_REST_H:g} 小时门槛，不值得为换票/行李重新托运折腾。"
            )
    elif excluded:
        # 所有候选都超时长护栏：最划算的那段也不值得飞
        headline = (
            "本轮不建议升舱：可选的升舱组合都会让全程多出 4 小时以上"
            "（主要耗在中转等待），优先考虑整程直飞票。"
        )
        reasons.append(_excluded_reason(min(excluded, key=_record_cost_key)))
    else:
        # 本来就没有多睡足够多的升舱选项
        headline = f"本轮不建议升舱：没有找到多睡 {MIN_EXTRA_REST_H:g} 小时以上的升舱组合。"
        reasons.append(
            f"所有可选组合里，没有哪个舱位组合能比全经济舱多睡 {MIN_EXTRA_REST_H:g} 小时以上，"
            "升舱没有意义。"
        )

    # 若同时存在「超时长」和「太贵/睡太少」两类，补一句说明超时长那类也没戏
    if reference and excluded:
        reasons.append(_excluded_reason(min(excluded, key=_record_cost_key)))

    # 全经济舱保底建议（不硬推升舱）
    if through_eco is not None:
        diff = base.total_price - through_eco
        word = "便宜" if diff <= 0 else "贵"
        reasons.append(
            f"若只求便宜：在{hub_cn}中转的全经济舱 {_fmt_money(base.total_price, currency)}，"
            f"比整程经济舱 {_fmt_money(through_eco, currency)}{word} "
            f"{_fmt_money(abs(diff), currency)}。"
        )
    else:
        reasons.append(
            f"若只求便宜：在{hub_cn}中转的全经济舱 {_fmt_money(base.total_price, currency)}，"
            f"共 {len(plan.legs)} 段。"
        )

    transfer_note = _transfer_note(base.total_ground_min, base.cabins)
    final_reason = (
        f"{_layover_text(plan, base)}，全程含中转约 {base.total_duration_min / 60.0:.1f} 小时。"
    )
    if transfer_note:
        final_reason += transfer_note
    reasons.append(final_reason)
    combo_dict = base.to_dict()
    combo_dict["transfer_note"] = transfer_note
    return {
        "headline": headline,
        "reasons": reasons[:4],
        "combo": combo_dict,
        "upgrade_if_forced": upgrade_if_forced,
        "warnings": _warning_list(plan, base, base, through_eco, currency),
    }

def _warning_list(
    plan: SplitPlan,
    baseline_combo: Optional[Combo],
    chosen_combo: Optional[Combo],
    through_eco: Optional[float],
    currency: str,
) -> list[str]:
    """§5.5 强制风险提示 + 本次方案的动态风险（都是用户决策前必须知道的）。

    `baseline_combo` 是全经济舱锚点（用于「拆票是否省钱」）；`chosen_combo`
    是实际推荐组合（用于判断实际中转是否偏紧，不同舱位可能换了班次）。
    """
    warnings = list(RISK_WARNINGS)
    if (
        baseline_combo is not None
        and through_eco is not None
        and baseline_combo.total_price > through_eco + 1e-6
    ):
        diff = baseline_combo.total_price - through_eco
        warnings.insert(
            0,
            f"注意：拆分后全经济舱 {_fmt_money(baseline_combo.total_price, currency)}，"
            f"比整程经济舱 {_fmt_money(through_eco, currency)}贵 "
            f"{_fmt_money(diff, currency)}，拆票本身不省钱，只为休息价值/分段灵活性。",
        )
    # 方案级衔接（经济舱锚点）+ 推荐组合自己的实际衔接，取更紧的那个提示
    gaps = list(plan.layovers_min or [])
    if chosen_combo is not None and len(getattr(plan, "legs", None) or []) == 2:
        if chosen_combo.total_ground_min > 0:
            gaps.append(chosen_combo.total_ground_min)
    if gaps:
        shortest = min(gaps)
        if shortest < TIGHT_CONNECT_MIN:
            warnings.append(
                f"最短衔接只有 {shortest:.0f} 分钟（低于 120 分钟偏紧），"
                "前段延误时误机风险较高，建议改选更长衔接的班次。"
            )
    if "换机场" in (plan.note or ""):
        warnings.append("该方案需要在两个机场之间换乘，地面转移时间请自行额外预留。")
    return warnings

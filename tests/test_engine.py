"""engine 模块单元测试（标准库 unittest，无第三方依赖）。

运行方式（在工作区根目录执行，不要用 pytest 也一定能跑）：
    python tests/test_engine.py

覆盖 SPEC §5 的关键语义：
  * 夜间长途 vs 白天短途的 rest_score 次序；
  * cost_per_rest_hour 手算校验；
  * 两段拆分方案的组合数、基准差价、衔接窗口、日期窗口、无经济舱丢弃；
  * 帕累托非支配性；
  * 只升一段的 best_value 选择；
  * 绕行比过滤 / 候选枢纽排序与数量；
  * recommend 的数字理由与 §5.5 强制风险提示。
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# 允许 `python tests/test_engine.py` 直接运行：把工作区根目录放上 sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flightwatch import airports
from flightwatch.engine import (
    annotate,
    build_combos,
    build_plan,
    build_plans,
    candidate_hubs,
    cost_per_rest_hour,
    effective_rest_h,
    night_ratio,
    recommend,
    rest_detail,
    rest_score,
    sleep_window_h,
)
from flightwatch.models import Leg, LegChoice, Query, Segment, SplitPlan, parse_dt


# --------------------------------------------------------------------------- 测试夹具
def make_leg(
    origin: str,
    dest: str,
    depart: str,
    duration_min: int,
    price: float,
    cabin: str = "ECONOMY",
    *,
    arrive: str | None = None,
    stops: int = 0,
    self_transfer: bool = False,
    with_segments: bool = True,
) -> Leg:
    """造一个票段；arrive_local 按时区差自动推算（模拟 provider 行为）。"""
    dep = parse_dt(depart)
    if dep.tzinfo is not None:
        dep = dep.replace(tzinfo=None)
    if arrive is None:
        shift_min = int(round(airports.tz_shift_h(origin, dest) * 60))
        arrive_dt = dep + timedelta(minutes=duration_min + shift_min)
        arrive = arrive_dt.strftime("%Y-%m-%dT%H:%M:%S")
    segments = []
    if with_segments:
        segments.append(
            Segment(
                carrier="XX",
                flight_no="100",
                origin=origin,
                destination=dest,
                depart_local=depart,
                arrive_local=arrive,
                duration_min=duration_min,
                cabin=cabin,
            )
        )
    return Leg(
        origin=origin,
        destination=dest,
        depart_local=depart,
        arrive_local=arrive,
        duration_min=duration_min,
        price=float(price),
        currency="CNY",
        cabin=cabin,
        segments=segments,
        provider="test",
        stops=stops,
        self_transfer=self_transfer,
    )


def _daily(depart_hm: str, duration_min: int, price: float, cabin: str = "ECONOMY", **kwargs):
    """返回 (date) -> [Leg] 的工厂：每天同一时刻一班。"""

    def factory(origin: str, dest: str):
        def build(day: str) -> list[Leg]:
            return [
                make_leg(
                    origin, dest, f"{day}T{depart_hm}:00", duration_min, price, cabin, **kwargs
                )
            ]

        return build

    return factory


class StubFetch:
    """可注入的 fetch：按 (origin, dest, cabin) 查表，表值是 date -> list[Leg] 的工厂。"""

    def __init__(self, table: dict | None = None):
        self.table = dict(table or {})
        self.calls: list[tuple[str, str, str, str]] = []

    def __call__(self, origin: str, dest: str, date: str, cabin: str) -> list[Leg]:
        self.calls.append((origin, dest, date, cabin))
        factory = self.table.get((origin, dest, cabin))
        return list(factory(date)) if factory else []


def standard_table() -> dict:
    """BJS -> IST -> JFK：夜间长途 + 长衔接，经济舱/公务舱都有报价。"""
    return {
        ("BJS", "IST", "ECONOMY"): _daily("02:00", 660, 2100.0, "ECONOMY")("BJS", "IST"),
        ("BJS", "IST", "BUSINESS"): _daily("02:30", 660, 9000.0, "BUSINESS")("BJS", "IST"),
        ("IST", "JFK", "ECONOMY"): _daily("14:00", 720, 2600.0, "ECONOMY")("IST", "JFK"),
        ("IST", "JFK", "BUSINESS"): _daily("15:00", 720, 18000.0, "BUSINESS")("IST", "JFK"),
    }


def _choice(index: int, origin: str, dest: str, options: dict, distance_km: float = 5000.0,
            tz_shift_h: float = 0.0) -> LegChoice:
    return LegChoice(
        index=index,
        origin=origin,
        destination=dest,
        distance_km=distance_km,
        tz_shift_h=tz_shift_h,
        options=options,
    )


def simple_plan() -> SplitPlan:
    """两段各 2 个舱位报价的手工方案（供 optimize 测试）。"""
    eco0 = make_leg("BJS", "IST", "2026-12-10T02:00:00", 660, 2000.0, "ECONOMY")
    biz0 = make_leg("BJS", "IST", "2026-12-10T02:00:00", 660, 6000.0, "BUSINESS")
    eco1 = make_leg("IST", "JFK", "2026-12-10T14:00:00", 720, 3000.0, "ECONOMY")
    biz1 = make_leg("IST", "JFK", "2026-12-10T15:00:00", 720, 6000.0, "BUSINESS")
    return SplitPlan(
        hub="IST",
        label="北京 → 伊斯坦布尔 + 伊斯坦布尔 → 纽约",
        legs=[
            _choice(0, "BJS", "IST", {"ECONOMY": eco0, "BUSINESS": biz0}),
            _choice(1, "IST", "JFK", {"ECONOMY": eco1, "BUSINESS": biz1}),
        ],
        layovers_min=[360.0],
        note="同一机场中转",
    )


# --------------------------------------------------------------------------- score.py
class ScoreTests(unittest.TestCase):
    def test_night_long_beats_day_short(self):
        """夜间长途（商务舱）分数应远高于白天短途（经济舱），且过 90 分。"""
        night = make_leg("BJS", "JFK", "2026-12-10T21:00:00", 720, 5000.0, "BUSINESS")
        night_eco = make_leg("BJS", "JFK", "2026-12-10T21:00:00", 720, 1500.0, "ECONOMY")
        day_short = make_leg("BJS", "SHA", "2026-12-10T10:00:00", 120, 800.0, "ECONOMY")

        self.assertGreater(rest_score(night), 90.0)
        self.assertEqual(rest_score(day_short), 0.0)
        self.assertGreater(rest_score(night), rest_score(night_eco))
        self.assertLess(rest_score(day_short), rest_score(night_eco))

    def test_effective_rest_h_hand_calculated(self):
        """手算：23:00 起飞、11 小时航段的睡眠窗口是 23:30~次日 09:15，夜间占比 7.5h。"""
        leg = make_leg("BJS", "JFK", "2026-12-10T23:00:00", 660, 8000.0, "BUSINESS")
        expected_base = 7.5  # 23:30 ~ 07:00
        expected_factor = 0.15 + 0.85 * (expected_base / 11.0)

        self.assertAlmostEqual(sleep_window_h(leg), expected_base, places=6)
        self.assertAlmostEqual(night_ratio(leg), expected_base / 11.0, places=6)
        # cabin=None 时用 leg.cabin（BUSINESS，系数 1.0）
        self.assertAlmostEqual(effective_rest_h(leg), expected_base * expected_factor, places=6)
        self.assertAlmostEqual(
            effective_rest_h(leg, "ECONOMY"), expected_base * expected_factor * 0.35, places=6
        )
        self.assertAlmostEqual(rest_score(leg), 68.39, places=1)
        self.assertLessEqual(rest_score(leg, "FIRST_CLASS"), 100.0)

    def test_sleep_window_ignores_timezone_offset(self):
        """带 +08:00 偏移的时间串应按墙钟时间算，结果与无偏移一致。"""
        naive = make_leg("BJS", "JFK", "2026-12-10T23:00:00", 660, 8000.0, "BUSINESS")
        aware = make_leg(
            "BJS", "JFK", "2026-12-10T23:00:00+08:00", 660, 8000.0, "BUSINESS",
            arrive="2026-12-11T10:00:00+08:00",
        )
        self.assertAlmostEqual(sleep_window_h(naive), sleep_window_h(aware), places=6)

    def test_cost_per_rest_hour_hand_calculated(self):
        """夜间同班次：多花 6000 元多换约 3.56 小时有效休息 → 约 1687 元/小时。"""
        eco = make_leg("BJS", "JFK", "2026-12-10T23:00:00", 660, 2000.0, "ECONOMY")
        biz = make_leg("BJS", "JFK", "2026-12-10T23:00:00", 660, 8000.0, "BUSINESS")

        expected = 6000.0 / (effective_rest_h(biz) - effective_rest_h(eco))
        self.assertAlmostEqual(cost_per_rest_hour(eco, biz), expected, places=6)
        self.assertAlmostEqual(cost_per_rest_hour(eco, biz), 1687.0, delta=1.5)  # 手算常量

    def test_cost_per_rest_hour_denominator_floor(self):
        """白天两段休息增益=0：分母用 0.15 兜底，300/0.15 = 2000 元/小时。"""
        eco = make_leg("BJS", "SHA", "2026-12-10T10:00:00", 120, 800.0, "ECONOMY")
        biz = make_leg("BJS", "SHA", "2026-12-10T10:00:00", 120, 1100.0, "BUSINESS")
        self.assertAlmostEqual(cost_per_rest_hour(eco, biz), 300.0 / 0.15, places=6)

    def test_cost_per_rest_hour_none_when_missing(self):
        leg = make_leg("BJS", "SHA", "2026-12-10T10:00:00", 120, 800.0)
        self.assertIsNone(cost_per_rest_hour(None, leg))
        self.assertIsNone(cost_per_rest_hour(leg, None))

    def test_rest_detail_shape(self):
        leg = make_leg("BJS", "JFK", "2026-12-10T23:00:00", 660, 8000.0, "BUSINESS")
        detail = rest_detail(leg)
        self.assertIn("score", detail)
        self.assertIn("verdict", detail)
        self.assertIn("ECONOMY", detail["effective_rest_h"])
        self.assertIn("BUSINESS", detail["effective_rest_h"])
        self.assertAlmostEqual(detail["sleep_h"], 7.5, places=2)
        self.assertAlmostEqual(detail["night_ratio"], 0.68, places=2)
        self.assertGreaterEqual(detail["score"], 0.0)
        self.assertLessEqual(detail["score"], 100.0)


# --------------------------------------------------------------------------- split.py
class SplitTests(unittest.TestCase):
    def test_candidate_hubs_filter_and_sort(self):
        """过滤同城枢纽、绕行比 >1.6 的枢纽，并按绕行比升序。"""
        q = Query(
            origin="BJS",
            destination="JFK",
            date="2026-12-10",
            hubs=["BJS", "JFK", "IST", "DOH", "SIN", "LAX", "DXB"],
        )
        hubs = candidate_hubs(q)
        # 绕行比：LAX 1.274 < IST 1.376 < DXB 1.533 < DOH 1.540；SIN 1.802 被过滤
        self.assertEqual(hubs, ["LAX", "IST", "DXB", "DOH"])
        self.assertNotIn("SIN", hubs)
        self.assertNotIn("BJS", hubs)
        self.assertNotIn("JFK", hubs)

    def test_candidate_hubs_default_pool_capped_at_four(self):
        """默认池按绕行比取前 4 个（BJS->JFK 最近的绕行是 YYZ/ORD/ICN/HEL）。"""
        q = Query(origin="BJS", destination="JFK", date="2026-12-10")
        hubs = candidate_hubs(q)
        self.assertEqual(len(hubs), 4)
        self.assertEqual(hubs, ["YYZ", "ORD", "ICN", "HEL"])
        for hub in hubs:
            self.assertGreaterEqual(airports.distance_km(q.origin, hub), 300.0)
            self.assertGreaterEqual(airports.distance_km(hub, q.destination), 300.0)
            ratio = (
                airports.distance_km(q.origin, hub) + airports.distance_km(hub, q.destination)
            ) / airports.distance_km(q.origin, q.destination)
            self.assertLessEqual(ratio, 1.6)

    def test_short_route_skips_detour_check(self):
        """整程 <500km 时跳过绕行比检查（BJS->TSN 只有 114km，SHA 仍可用）。"""
        q = Query(origin="BJS", destination="TSN", date="2026-12-10", hubs=["SHA"])
        self.assertEqual(candidate_hubs(q), ["SHA"])

    def test_build_plan_basic(self):
        fetch = StubFetch(standard_table())
        q = Query(origin="BJS", destination="JFK", date="2026-12-10", hubs=["IST"])
        plan = build_plan(q, "IST", fetch)

        self.assertIsNotNone(plan)
        self.assertEqual(plan.hub, "IST")
        self.assertEqual(len(plan.legs), 2)
        self.assertIn("伊斯坦布尔", plan.label)
        self.assertIn("行李", plan.note)
        self.assertIn("中转", plan.note)
        self.assertEqual(plan.layovers_min, [360.0])  # 08:00 落地 -> 14:00 起飞

        first, second = plan.legs
        self.assertEqual(first.options["ECONOMY"].price, 2100.0)
        self.assertEqual(first.options["BUSINESS"].price, 9000.0)
        self.assertEqual(second.options["ECONOMY"].price, 2600.0)
        self.assertEqual(second.options["BUSINESS"].price, 18000.0)
        # v1 关闭超经：不应出现 PREMIUM_ECONOMY 报价
        self.assertNotIn("PREMIUM_ECONOMY", first.options)
        self.assertNotIn("PREMIUM_ECONOMY", second.options)
        # 每段查了 date 与 date+1，并且没有白查超经
        queried = {(o, d, day, cabin) for o, d, day, cabin in fetch.calls}
        self.assertIn(("BJS", "IST", "2026-12-10", "ECONOMY"), queried)
        self.assertIn(("BJS", "IST", "2026-12-11", "ECONOMY"), queried)
        self.assertIn(("IST", "JFK", "2026-12-11", "BUSINESS"), queried)
        self.assertFalse(any(cabin == "PREMIUM_ECONOMY" for _, _, _, cabin in fetch.calls))

    def test_build_plan_uses_next_day_window(self):
        """第一段当天没票、只有次日有票时，第二段应自动选次日的可行衔接。"""
        table = {
            # 当天无票，只有 date+1 才有（跨境长航线次日出发的典型情况）
            ("BJS", "IST", "ECONOMY"): lambda day: (
                [] if day == "2026-12-10"
                else [make_leg("BJS", "IST", f"{day}T02:00:00", 660, 2100.0, "ECONOMY")]
            ),
            ("IST", "JFK", "ECONOMY"): _daily("14:00", 720, 2600.0, "ECONOMY")("IST", "JFK"),
        }
        fetch = StubFetch(table)
        q = Query(origin="BJS", destination="JFK", date="2026-12-10", hubs=["IST"])
        plan = build_plan(q, "IST", fetch)

        self.assertIsNotNone(plan)
        self.assertTrue(plan.legs[0].options["ECONOMY"].depart_local.startswith("2026-12-11"))
        self.assertTrue(plan.legs[1].options["ECONOMY"].depart_local.startswith("2026-12-11"))
        self.assertEqual(plan.layovers_min, [360.0])

    def test_build_plan_drops_when_economy_missing(self):
        """某段完全没有经济舱 → 整个方案丢弃（经济舱是底价锚点）。"""
        table = standard_table()
        del table[("BJS", "IST", "ECONOMY")]
        fetch = StubFetch(table)
        q = Query(origin="BJS", destination="JFK", date="2026-12-10", hubs=["IST"])
        self.assertIsNone(build_plan(q, "IST", fetch))

    def test_build_plan_drops_when_connection_infeasible(self):
        """衔接为负（第二段在落地前就起飞）→ 方案不可行。"""
        table = {
            ("BJS", "IST", "ECONOMY"): _daily("02:00", 660, 2100.0, "ECONOMY")("BJS", "IST"),
            ("IST", "JFK", "ECONOMY"): _daily("05:00", 720, 2600.0, "ECONOMY")("IST", "JFK"),
        }
        fetch = StubFetch(table)
        q = Query(origin="BJS", destination="JFK", date="2026-12-10", hubs=["IST"])
        self.assertIsNone(build_plan(q, "IST", fetch))

    def test_build_plan_respects_self_transfer_flag(self):
        """allow_self_transfer=False 时，伪联程（自理行李）票段应被过滤。"""
        table = {
            ("BJS", "IST", "ECONOMY"): _daily(
                "02:00", 660, 2100.0, "ECONOMY", self_transfer=True
            )("BJS", "IST"),
            ("IST", "JFK", "ECONOMY"): _daily("14:00", 720, 2600.0, "ECONOMY")("IST", "JFK"),
        }
        fetch = StubFetch(table)
        q = Query(
            origin="BJS", destination="JFK", date="2026-12-10", hubs=["IST"],
            allow_self_transfer=False,
        )
        self.assertIsNone(build_plan(q, "IST", fetch))

    def test_build_plans_and_progress(self):
        fetch = StubFetch(standard_table())
        q = Query(origin="BJS", destination="JFK", date="2026-12-10", hubs=["IST", "DOH"])
        progress_calls: list[tuple[int, int, str]] = []
        plans = build_plans(
            q, fetch, progress=lambda done, total, label: progress_calls.append((done, total, label))
        )

        self.assertEqual(len(plans), 1)  # DOH 没有报价
        self.assertEqual(plans[0].hub, "IST")
        self.assertTrue(progress_calls)
        self.assertEqual(progress_calls[-1][:2], (2, 2))
        self.assertTrue(any("伊斯坦布尔" in call[2] for call in progress_calls))
        # DOH 确实被查询过（说明不是提前退出）
        self.assertTrue(any(call[1] == "DOH" for call in fetch.calls))
        # progress=None 也要能跑
        self.assertEqual(len(build_plans(q, StubFetch(standard_table()), hubs=["IST"])), 1)
        # 回调自身抛异常不能影响计算结果（api.py 的进度展示可能出错）
        plans2 = build_plans(
            q,
            StubFetch(standard_table()),
            hubs=["IST"],
            progress=lambda done, total, label: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        self.assertEqual(len(plans2), 1)

    def test_build_plans_explicit_hubs_override_query(self):
        """显式 hubs 参数覆盖 Query.hubs，且不做数量截断。"""
        fetch = StubFetch(standard_table())
        q = Query(origin="BJS", destination="JFK", date="2026-12-10")  # q.hubs 为空
        plans = build_plans(q, fetch, hubs=["IST", "DOH", "LAX"])
        self.assertEqual([p.hub for p in plans], ["IST"])
        self.assertTrue(any(call[1] == "LAX" for call in fetch.calls))


# --------------------------------------------------------------------------- optimize.py
class OptimizeTests(unittest.TestCase):
    def test_build_combos_count_and_baseline_delta(self):
        """两段 × 2 舱位 = 4 个组合；差价/休息增益相对全经济舱。"""
        plan = simple_plan()
        combos = build_combos(plan)
        self.assertEqual(len(combos), 4)
        self.assertEqual(combos[0].cabins, ["ECONOMY", "ECONOMY"])

        base = combos[0]
        self.assertEqual(base.total_price, 5000.0)               # 2000 + 3000
        self.assertEqual(base.total_ground_min, 360.0)
        self.assertEqual(base.total_duration_min, 660 + 720 + 360.0)
        self.assertEqual(base.price_delta, 0.0)
        self.assertEqual(base.rest_gain, 0.0)
        self.assertIsNone(base.value_per_rest_hour)
        self.assertEqual(base.currency, "CNY")

        eco_biz = next(c for c in combos if c.cabins == ["ECONOMY", "BUSINESS"])
        self.assertEqual(eco_biz.total_price, 2000.0 + 6000.0)
        self.assertAlmostEqual(eco_biz.price_delta, 3000.0)
        expected_gain = effective_rest_h(
            plan.legs[1].options["BUSINESS"], "BUSINESS"
        ) - effective_rest_h(plan.legs[1].options["ECONOMY"], "ECONOMY")
        self.assertAlmostEqual(eco_biz.rest_gain, expected_gain)
        self.assertAlmostEqual(eco_biz.value_per_rest_hour, 3000.0 / expected_gain)

    def test_build_combos_drops_leg_without_options(self):
        plan = simple_plan()
        plan.legs[1].options = {}
        self.assertEqual(build_combos(plan), [])
        plan.legs[0].options = {}
        self.assertEqual(build_combos(plan), [])

    def test_combo_ground_follows_actual_flights(self):
        """回归：不同舱位选到的不是同一班飞机时，每个组合必须用自己的衔接时长。

        经济舱第一段 12-10T02:00 起飞、08:00 落地 IST；公务舱第一段是
        12-09T18:40 起飞、12-10T00:40 落地 IST。第二段都是 14:00 起飞，
        所以 经济+经济 衔接 360min，公务+经济 实际衔接 800min。
        """
        eco0 = make_leg("BJS", "IST", "2026-12-10T02:00:00", 660, 2000.0, "ECONOMY")
        eco1 = make_leg("IST", "JFK", "2026-12-10T14:00:00", 720, 3000.0, "ECONOMY")
        biz0 = make_leg("BJS", "IST", "2026-12-09T18:40:00", 660, 4000.0, "BUSINESS")
        biz1 = make_leg("IST", "JFK", "2026-12-10T14:00:00", 720, 6000.0, "BUSINESS")
        plan = SplitPlan(
            hub="IST",
            label="test",
            legs=[
                _choice(0, "BJS", "IST", {"ECONOMY": eco0, "BUSINESS": biz0}),
                _choice(1, "IST", "JFK", {"ECONOMY": eco1, "BUSINESS": biz1}),
            ],
            layovers_min=[360.0],
            note="同一机场中转",
        )

        combos = {tuple(c.cabins): c for c in build_combos(plan)}
        eco_eco = combos[("ECONOMY", "ECONOMY")]
        biz_eco = combos[("BUSINESS", "ECONOMY")]
        self.assertEqual(eco_eco.total_ground_min, 360.0)   # 14:00 - 08:00
        self.assertEqual(biz_eco.total_ground_min, 800.0)   # 14:00 - 00:40
        self.assertNotEqual(eco_eco.total_ground_min, biz_eco.total_ground_min)
        self.assertAlmostEqual(biz_eco.total_duration_min, 660 + 720 + 800.0)
        # 全经济舱组合的衔接与方案锚点一致
        self.assertEqual(eco_eco.total_ground_min, plan.layovers_min[0])

        # annotate 输出：每个组合带自己的 ground、时长护栏标记与超长中转说明
        ann = annotate(plan)
        rows = {tuple(row["cabins"]): row for row in ann["combos"]}
        self.assertEqual(rows[("BUSINESS", "ECONOMY")]["total_ground_min"], 800)
        self.assertIn("中转等待", rows[("BUSINESS", "ECONOMY")]["transfer_note"])
        self.assertIn("13.3", rows[("BUSINESS", "ECONOMY")]["transfer_note"])  # 800min = 13.3h
        self.assertEqual(rows[("ECONOMY", "ECONOMY")]["transfer_note"], "")
        # 时长护栏：公务+经济 比全经济多 440 分钟（>240）→ too_long=True
        self.assertAlmostEqual(rows[("BUSINESS", "ECONOMY")]["duration_delta_min"], 440.0)
        self.assertTrue(rows[("BUSINESS", "ECONOMY")]["too_long"])
        self.assertFalse(rows[("ECONOMY", "ECONOMY")]["too_long"])
        self.assertAlmostEqual(rows[("ECONOMY", "ECONOMY")]["duration_delta_min"], 0.0)
        self.assertFalse(rows[("ECONOMY", "BUSINESS")]["too_long"])  # 第二段没换班次
        # best_value 跳过 too_long 的组合：只剩第 2 段升公务舱可选
        best = ann["best_value"]
        self.assertEqual(best["leg_index"], 1)
        self.assertEqual(best["cabin"], "BUSINESS")
        # recommend 不会推荐 43.9 小时那个组合，并在理由里说明它被排除
        rec = recommend([plan], {"through_economy": {"price": 9000.0}})
        self.assertEqual(rec["combo"]["cabins"], ["ECONOMY", "BUSINESS"])
        self.assertEqual(rec["combo"]["total_ground_min"], 360)
        reasons_blob = " ".join(rec["reasons"])
        self.assertIn("已排除", reasons_blob)
        self.assertIn("7.3", reasons_blob)  # 440min ≈ 7.3 小时

    def test_duration_guardrail_allows_small_extra(self):
        """只多 2 小时（120min ≤ 240min 护栏）的升舱组合仍应被推荐。"""
        eco0 = make_leg("BJS", "IST", "2026-12-10T02:00:00", 660, 2000.0, "ECONOMY")
        eco1 = make_leg("IST", "JFK", "2026-12-10T14:00:00", 720, 3000.0, "ECONOMY")
        # 公务舱第一段 00:00 起飞、06:00 落地 -> 衔接 480min，比全经济舱(360)只多 120min
        biz0 = make_leg("BJS", "IST", "2026-12-10T00:00:00", 660, 4000.0, "BUSINESS")
        biz1 = make_leg("IST", "JFK", "2026-12-10T14:00:00", 720, 6000.0, "BUSINESS")
        plan = SplitPlan(
            hub="IST",
            label="test",
            legs=[
                _choice(0, "BJS", "IST", {"ECONOMY": eco0, "BUSINESS": biz0}),
                _choice(1, "IST", "JFK", {"ECONOMY": eco1, "BUSINESS": biz1}),
            ],
            layovers_min=[360.0],
            note="同一机场中转",
        )
        combos = {tuple(c.cabins): c for c in build_combos(plan)}
        biz_eco = combos[("BUSINESS", "ECONOMY")]
        self.assertEqual(biz_eco.total_ground_min, 480.0)  # 14:00 - 06:00
        self.assertFalse(biz_eco.too_long)

        ann = annotate(plan)
        rows = {tuple(row["cabins"]): row for row in ann["combos"]}
        self.assertAlmostEqual(rows[("BUSINESS", "ECONOMY")]["duration_delta_min"], 120.0)
        self.assertFalse(rows[("BUSINESS", "ECONOMY")]["too_long"])
        self.assertEqual(ann["best_value"]["leg_index"], 0)

        rec = recommend([plan], {"through_economy": {"price": 9000.0}})
        self.assertEqual(rec["combo"]["cabins"], ["BUSINESS", "ECONOMY"])
        self.assertEqual(rec["combo"]["total_ground_min"], 480)
        self.assertNotIn("已排除", " ".join(rec["reasons"]))

    def test_duration_guardrail_all_too_long_falls_back(self):
        """所有升舱组合都超时长护栏 → best_value=None，recommend 明确不建议升舱。"""
        eco0 = make_leg("BJS", "IST", "2026-12-10T02:00:00", 660, 2000.0, "ECONOMY")
        eco1 = make_leg("IST", "JFK", "2026-12-10T14:00:00", 720, 3000.0, "ECONOMY")
        # 公务舱第一段提前一天 18:40 起飞 -> 实际衔接 800min，全程多 440min
        biz0 = make_leg("BJS", "IST", "2026-12-09T18:40:00", 660, 4000.0, "BUSINESS")
        plan = SplitPlan(
            hub="IST",
            label="test",
            legs=[
                _choice(0, "BJS", "IST", {"ECONOMY": eco0, "BUSINESS": biz0}),
                _choice(1, "IST", "JFK", {"ECONOMY": eco1}),  # 第二段没有公务舱
            ],
            layovers_min=[360.0],
            note="同一机场中转",
        )
        ann = annotate(plan)
        self.assertIsNone(ann["best_value"])

        rec = recommend([plan], {"through_economy": {"price": 9000.0}})
        self.assertIn("本轮不建议升舱", rec["headline"])
        self.assertEqual(rec["combo"]["cabins"], ["ECONOMY", "ECONOMY"])
        self.assertIsNone(rec["upgrade_if_forced"])  # 全部 too_long → 没有「强制升舱」选项
        self.assertGreaterEqual(len(rec["reasons"]), 1)
        self.assertLessEqual(len(rec["reasons"]), 4)
        blob = " ".join(rec["reasons"])
        self.assertIn("已排除", blob)
        for reason in rec["reasons"]:
            self.assertTrue(any(ch.isdigit() for ch in reason), reason)
        self.assertGreaterEqual(len(rec["warnings"]), 4)

    def test_absolute_gate_rejects_expensive_small_gain(self):
        """¥36,882/小时 + 多睡 0.6h 的候选不合格：headline 明确不建议升舱，但仍给「强制升舱」参考。"""
        eco0 = make_leg("BJS", "IST", "2026-12-10T02:00:00", 660, 2000.0, "ECONOMY")
        eco1 = make_leg("IST", "JFK", "2026-12-10T10:00:00", 600, 2000.0, "ECONOMY")
        # 公务舱第二段 04:30 起飞、10h 航段：比白天经济舱只多睡约 0.64h，却贵 23600 元
        biz1 = make_leg("IST", "JFK", "2026-12-10T04:30:00", 600, 25600.0, "BUSINESS")
        plan = SplitPlan(
            hub="IST",
            label="test",
            legs=[
                _choice(0, "BJS", "IST", {"ECONOMY": eco0}),
                _choice(1, "IST", "JFK", {"ECONOMY": eco1, "BUSINESS": biz1}),
            ],
            layovers_min=[120.0],
            note="同一机场中转",
        )
        ann = annotate(plan)
        self.assertIsNone(ann["best_value"])  # 不合格 → 不做「最值得升舱」
        rows = {tuple(row["cabins"]): row for row in ann["combos"]}
        upgrade_row = rows[("ECONOMY", "BUSINESS")]
        self.assertGreater(upgrade_row["value_per_rest_hour"], 4000.0)
        self.assertLess(upgrade_row["rest_gain"], 1.0)

        rec = recommend([plan], {"through_economy": {"price": 9000.0}})
        self.assertIn("本轮不建议升舱", rec["headline"])
        self.assertEqual(rec["combo"]["cabins"], ["ECONOMY", "ECONOMY"])
        # 「如果一定要升」仍指向这段相对最划算的，但不写进 headline
        forced = rec["upgrade_if_forced"]
        self.assertIsNotNone(forced)
        self.assertEqual(forced["leg_index"], 1)
        self.assertEqual(forced["cabin"], "BUSINESS")
        self.assertGreater(forced["cost_per_rest_h"], 4000.0)
        self.assertLess(forced["extra_rest_h"], 1.0)
        self.assertNotIn("最划算：", rec["headline"])  # 不硬推
        blob = " ".join(rec["reasons"])
        self.assertIn("心理价位", blob)
        self.assertIn("4,000", blob)
        for reason in rec["reasons"]:
            self.assertTrue(any(ch.isdigit() for ch in reason), reason)

    def test_absolute_gate_accepts_1953_per_hour(self):
        """¥1,953/小时、多睡约 4.9h 的候选合格，应正常推荐（对应离线验收的 IST 第2段）。"""
        eco0 = make_leg("BJS", "IST", "2026-12-10T02:00:00", 660, 2000.0, "ECONOMY")
        # 经济舱第二段白天 10:00 起飞、9h 航段 → 有效休息 0
        eco1 = make_leg("IST", "JFK", "2026-12-10T10:00:00", 540, 2000.0, "ECONOMY")
        # 公务舱第二段 00:00 起飞、9h 航段 → 夜间可睡 6.5h，折算有效休息约 4.97h
        biz1 = make_leg("IST", "JFK", "2026-12-10T00:00:00", 540, 11696.1, "BUSINESS")
        plan = SplitPlan(
            hub="IST",
            label="test",
            legs=[
                _choice(0, "BJS", "IST", {"ECONOMY": eco0}),
                _choice(1, "IST", "JFK", {"ECONOMY": eco1, "BUSINESS": biz1}),
            ],
            layovers_min=[120.0],
            note="同一机场中转",
        )
        ann = annotate(plan)
        self.assertIsNotNone(ann["best_value"])
        self.assertEqual(ann["best_value"]["leg_index"], 1)
        self.assertEqual(ann["best_value"]["cabin"], "BUSINESS")
        self.assertAlmostEqual(ann["best_value"]["cost_per_rest_h"], 1952.7, delta=1.0)
        self.assertGreaterEqual(ann["best_value"]["extra_rest_h"], 1.0)

        rec = recommend(
            [plan],
            {"through_economy": {"price": 9000.0}, "through_business": {"price": 30000.0}},
        )
        self.assertIn("最划算", rec["headline"])
        self.assertEqual(rec["combo"]["cabins"], ["ECONOMY", "BUSINESS"])
        self.assertIsNotNone(rec["upgrade_if_forced"])
        self.assertEqual(rec["upgrade_if_forced"]["leg_index"], 1)
        self.assertLessEqual(rec["upgrade_if_forced"]["cost_per_rest_h"], 4000.0)
        self.assertIn("元/小时", " ".join(rec["reasons"]))

    def test_pareto_non_dominated(self):
        """构造一个被支配的组合，验证帕累托只保留非支配点且按总价升序。"""
        eco0 = make_leg("BJS", "IST", "2026-12-10T10:00:00", 120, 1000.0, "ECONOMY")
        biz0 = make_leg("BJS", "IST", "2026-12-10T22:00:00", 720, 4000.0, "BUSINESS")
        eco1 = make_leg("IST", "JFK", "2026-12-10T10:00:00", 120, 1000.0, "ECONOMY")
        biz1 = make_leg("IST", "JFK", "2026-12-10T22:00:00", 300, 5000.0, "BUSINESS")
        plan = SplitPlan(
            hub="IST",
            label="test",
            legs=[
                _choice(0, "BJS", "IST", {"ECONOMY": eco0, "BUSINESS": biz0}),
                _choice(1, "IST", "JFK", {"ECONOMY": eco1, "BUSINESS": biz1}),
            ],
            layovers_min=[360.0],
            note="",
        )
        combos = build_combos(plan)
        self.assertEqual(len(combos), 4)
        # 组合顺序：0=ECO/ECO, 1=ECO/BIZ(6000), 2=BIZ/ECO(5000), 3=BIZ/BIZ
        self.assertGreater(combos[2].rest_gain, combos[1].rest_gain)  # biz0 夜间休息更多
        self.assertLess(combos[2].total_price, combos[1].total_price)  # 而且比它更便宜
        # 2 支配 1：更便宜且休息更多
        self.assertGreaterEqual(combos[2].rest_gain, combos[1].rest_gain)
        self.assertLessEqual(combos[2].total_price, combos[1].total_price)

        ann = annotate(plan)
        self.assertEqual(ann["pareto"], [0, 2, 3])
        # 逐点验证非支配性
        for i in ann["pareto"]:
            for j, other in enumerate(combos):
                if i == j:
                    continue
                dominated = (
                    other.total_price <= combos[i].total_price
                    and other.rest_gain >= combos[i].rest_gain
                    and (
                        other.total_price < combos[i].total_price
                        or other.rest_gain > combos[i].rest_gain
                    )
                )
                self.assertFalse(dominated, f"组合 {i} 被组合 {j} 支配")
        # 返回的下标按总价升序
        prices = [combos[i].total_price for i in ann["pareto"]]
        self.assertEqual(prices, sorted(prices))
        # annotate 结果必须能直接 json 序列化（api.py 直接塞进响应）
        json.dumps(ann, ensure_ascii=False)

    def test_best_value_picks_single_upgrade(self):
        """只升一段：选 cost_per_rest_h 最小的那一段（本例是第 2 段）。"""
        plan = simple_plan()
        combos = build_combos(plan)
        base = combos[0]
        single_upgrades = []
        for combo in combos:
            changed = [i for i, cabin in enumerate(combo.cabins) if cabin != "ECONOMY"]
            if len(changed) != 1:
                continue
            extra_cost = combo.total_price - base.total_price
            extra_rest = combo.comfort_score - base.comfort_score
            single_upgrades.append(
                (extra_cost / max(extra_rest, 0.15), changed[0], extra_cost, extra_rest)
            )
        expected = min(single_upgrades, key=lambda item: (round(item[0], 6), -round(item[3], 6)))

        best = annotate(plan)["best_value"]
        self.assertIsNotNone(best)
        self.assertEqual(best["leg_index"], expected[1])
        self.assertEqual(best["leg_index"], 1)
        self.assertEqual(best["cabin"], "BUSINESS")
        self.assertAlmostEqual(best["extra_cost"], round(expected[2], 2), places=2)
        self.assertAlmostEqual(best["extra_rest_h"], round(expected[3], 2), places=2)
        self.assertAlmostEqual(best["cost_per_rest_h"], round(expected[0], 2), places=2)

    def test_best_value_none_when_no_upgrade_option(self):
        eco0 = make_leg("BJS", "IST", "2026-12-10T02:00:00", 660, 2000.0, "ECONOMY")
        eco1 = make_leg("IST", "JFK", "2026-12-10T14:00:00", 720, 3000.0, "ECONOMY")
        plan = SplitPlan(
            hub="IST",
            label="test",
            legs=[
                _choice(0, "BJS", "IST", {"ECONOMY": eco0}),
                _choice(1, "IST", "JFK", {"ECONOMY": eco1}),
            ],
            layovers_min=[360.0],
            note="",
        )
        ann = annotate(plan)
        self.assertEqual(len(ann["combos"]), 1)
        self.assertIsNone(ann["best_value"])
        self.assertEqual(ann["pareto"], [0])

    def test_recommend_headline_reasons_warnings(self):
        baseline = {
            "through_economy": {"price": 9000.0},
            "through_business": {"price": 30000.0},
        }
        rec = recommend([simple_plan()], baseline)

        self.assertIsInstance(rec["headline"], str)
        self.assertTrue(rec["headline"])
        self.assertGreaterEqual(len(rec["reasons"]), 1)
        self.assertLessEqual(len(rec["reasons"]), 4)
        for reason in rec["reasons"]:
            self.assertTrue(any(ch.isdigit() for ch in reason), f"理由必须含数字：{reason}")
        self.assertIsNotNone(rec["combo"])
        self.assertIn("cabins", rec["combo"])

        joined = " ".join(rec["warnings"])
        for keyword in ("联程票", "签证", "自愿弃程", "复核"):
            self.assertIn(keyword, joined)
        self.assertGreaterEqual(len(rec["warnings"]), 4)
        # 推荐应落在「升一段」的组合上（本数据夜间航段确实值得升）
        self.assertEqual(rec["combo"]["cabins"], ["ECONOMY", "BUSINESS"])
        self.assertIn("元/小时", " ".join(rec["reasons"]))
        # 「强制升舱」与推荐一致（都合格时）
        self.assertIsNotNone(rec["upgrade_if_forced"])
        self.assertEqual(rec["upgrade_if_forced"]["leg_index"], 1)
        self.assertEqual(rec["upgrade_if_forced"]["cabin"], "BUSINESS")

    def test_recommend_without_baseline(self):
        fetch = StubFetch(standard_table())
        q = Query(origin="BJS", destination="JFK", date="2026-12-10", hubs=["IST"])
        plans = build_plans(q, fetch)
        rec = recommend(plans, None)
        self.assertTrue(rec["headline"])
        self.assertIsNotNone(rec["combo"])
        self.assertIn("upgrade_if_forced", rec)
        for reason in rec["reasons"]:
            self.assertTrue(any(ch.isdigit() for ch in reason), reason)

    def test_recommend_falls_back_to_cheapest_economy(self):
        """没有任何值得升的段落时，推荐最便宜的全经济舱方案（不硬推升舱）。"""
        eco0 = make_leg("BJS", "IST", "2026-12-10T02:00:00", 660, 2000.0, "ECONOMY")
        eco1 = make_leg("IST", "JFK", "2026-12-10T14:00:00", 720, 3000.0, "ECONOMY")
        plan = SplitPlan(
            hub="IST",
            label="test",
            legs=[
                _choice(0, "BJS", "IST", {"ECONOMY": eco0}),
                _choice(1, "IST", "JFK", {"ECONOMY": eco1}),
            ],
            layovers_min=[360.0],
            note="同一机场中转",
        )
        rec = recommend([plan], {"through_economy": {"price": 9000.0}})
        self.assertEqual(rec["combo"]["cabins"], ["ECONOMY", "ECONOMY"])
        self.assertEqual(rec["combo"]["plan_hub"], "IST")
        self.assertIn("本轮不建议升舱", rec["headline"])
        self.assertIsNone(rec["upgrade_if_forced"])  # 本来就没有升舱选项
        self.assertGreaterEqual(len(rec["reasons"]), 1)
        self.assertLessEqual(len(rec["reasons"]), 4)
        for reason in rec["reasons"]:
            self.assertTrue(any(ch.isdigit() for ch in reason), reason)
        self.assertGreaterEqual(len(rec["warnings"]), 4)

    def test_recommend_empty_plans(self):
        rec = recommend([], None)
        self.assertIn("headline", rec)
        self.assertIsNone(rec["combo"])
        self.assertIsNone(rec["upgrade_if_forced"])
        self.assertTrue(rec["headline"])
        # §5.5 风险提示在无方案时也应给出
        self.assertGreaterEqual(len(rec["warnings"]), 4)


# --------------------------------------------------------------------------- 跨模块
class IntegrationTests(unittest.TestCase):
    def test_engine_public_api_is_usable_end_to_end(self):
        fetch = StubFetch(standard_table())
        q = Query(origin="BJS", destination="JFK", date="2026-12-10", hubs=["IST"])
        plans = build_plans(q, fetch)
        self.assertEqual(len(plans), 1)

        annotation = annotate(plans[0])
        self.assertEqual(len(annotation["combos"]), 4)
        self.assertEqual(annotation["combos"][0]["cabins"], ["ECONOMY", "ECONOMY"])
        self.assertEqual(annotation["combos"][0]["price_delta"], 0.0)
        self.assertIsInstance(annotation["pareto"], list)
        # 每个 pareto 下标都指向真实组合
        for index in annotation["pareto"]:
            self.assertLess(index, len(annotation["combos"]))

        rec = recommend(plans, {"through_economy": {"price": 9000.0}})
        self.assertEqual(rec["combo"]["plan_hub"], "IST")
        # 推荐组合必须是 annotate 里出现过的组合之一
        self.assertIn(
            rec["combo"]["cabins"],
            [combo["cabins"] for combo in annotation["combos"]],
        )
        # 整条链路都能 JSON 序列化（api.py 直接把 dict 交给 server）
        json.dumps(
            {
                "plans": [plan.to_dict() for plan in plans],
                "recommendation": rec,
            },
            ensure_ascii=False,
        )

    def test_build_plan_scores_are_consistent_between_api_helpers(self):
        """api.py 用 sleep_window_h/night_ratio/effective_rest_h 组装的数字必须能互相印证。"""
        leg = make_leg("BJS", "JFK", "2026-12-10T23:00:00", 660, 8000.0, "BUSINESS")
        duration_h = leg.duration_min / 60.0
        expected_sleep = sleep_window_h(leg)
        self.assertAlmostEqual(night_ratio(leg), expected_sleep / duration_h, places=9)
        recomputed = (
            min(duration_h, expected_sleep)
            * (0.15 + 0.85 * night_ratio(leg))
            * 1.0
        )
        self.assertAlmostEqual(effective_rest_h(leg), recomputed, places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)

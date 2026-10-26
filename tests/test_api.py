"""集成层测试：用离线数据源跑通「查询 -> JSON -> 报告」全流程。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flightwatch import api, report  # noqa: E402


class TestMakeQuery(unittest.TestCase):
    def test_resolve_chinese_and_airport_codes(self):
        q = api.make_query("北京", "JFK", "2026-12-10", hubs=["伊斯坦布尔", "doh"])
        self.assertEqual(q.origin, "BJS")
        self.assertEqual(q.destination, "JFK")
        self.assertEqual(q.hubs[:2], ["IST", "DOH"])

    def test_reject_bad_city(self):
        # allow_remote=False：不联网，纯本地校验
        with self.assertRaises(ValueError):
            api.make_query("火星基地", "纽约", "2026-12-10", allow_remote=False)

    def test_reject_same_city(self):
        with self.assertRaises(ValueError):
            api.make_query("北京", "PEK", "2026-12-10")

    def test_reject_bad_date(self):
        with self.assertRaises(ValueError):
            api.make_query("北京", "纽约", "2026/12/10")

    def test_hub_equal_to_endpoint_is_dropped(self):
        q = api.make_query("北京", "纽约", "2026-12-10", hubs=["BJS", "IST"])
        self.assertNotIn("BJS", q.hubs)


class TestRunOffline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.q = api.make_query("北京", "纽约", "2026-12-10", hubs=["IST", "DOH"])
        cls.payload = api.run(cls.q, offline=True, max_hubs=2)

    def test_payload_shape(self):
        p = self.payload
        for key in ("ok", "query", "source", "baseline", "plans", "recommendation", "warnings", "errors"):
            self.assertIn(key, p, f"缺少字段 {key}")
        # 必须能直接 json 序列化（Web 接口依赖这一点）
        json.dumps(p, ensure_ascii=False)

    def test_query_echo(self):
        q = self.payload["query"]
        self.assertEqual(q["origin"], "BJS")
        self.assertEqual(q["destination"], "JFK")
        self.assertEqual(q["origin_name"], "北京")

    def test_plans_complete(self):
        plans = self.payload["plans"]
        self.assertTrue(plans, "离线数据应当能拼出方案")
        plan = plans[0]
        for key in ("hub", "hub_name", "legs", "combos", "best_value", "pareto"):
            self.assertIn(key, plan)
        self.assertTrue(plan["legs"])
        for leg in plan["legs"]:
            self.assertIn("rest", leg)
            self.assertIn("options", leg)
            self.assertIsNotNone(leg["options"].get("ECONOMY"))

    def test_combos_include_all_economy_baseline(self):
        combos = self.payload["plans"][0]["combos"]
        all_eco = [c for c in combos if set(c["cabins"]) == {"ECONOMY"}]
        self.assertEqual(len(all_eco), 1)
        self.assertEqual(all_eco[0]["price_delta"], 0)
        self.assertEqual(all_eco[0]["rest_gain"], 0)

    def test_baseline_has_direct_comparison(self):
        baseline = self.payload["baseline"]
        self.assertIn("through_economy", baseline)
        self.assertIn("nonstop_economy", baseline, "基准里必须有直飞对比项")
        if baseline.get("nonstop_economy"):
            self.assertEqual(baseline["nonstop_economy"]["stops"], 0, "直飞项必须是 0 次中转")

    def test_recommendation_mentions_direct(self):
        reasons = (self.payload.get("recommendation") or {}).get("reasons") or []
        self.assertTrue(any("直飞" in r for r in reasons), f"结论里应包含直飞对比，实际={reasons}")

    def test_recommendation_has_headline(self):
        rec = self.payload["recommendation"]
        self.assertTrue(rec.get("headline"))
        self.assertIsInstance(rec.get("reasons"), list)
        self.assertIsInstance(rec.get("warnings"), list)

    def test_warnings_cover_separate_ticket_risk(self):
        blob = " ".join(self.payload["warnings"])
        for kw in ("联程", "行李", "签证"):
            self.assertIn(kw, blob)


class TestReport(unittest.TestCase):
    def test_render_console_and_html(self):
        q = api.make_query("北京", "纽约", "2026-12-10", hubs=["IST", "DOH"])
        payload = api.run(q, offline=True, max_hubs=2)
        text = report.render_console(payload)
        self.assertIn("北京", text)
        self.assertIn("经济舱", text)
        html = report.render_html(payload)
        self.assertIn("<html", html)
        self.assertIn("风险提示", html)
        self.assertNotIn("None", html.split("<body>")[1][:2000])


class TestServerRouting(unittest.TestCase):
    def test_mime_and_path_guard(self):
        from flightwatch import server

        self.assertIn("html", server._MIME[".html"])
        # 目录穿越必须被拦住：这里只验证路径拼接后的规范化逻辑
        base = os.path.normpath(server.WEB_DIR)
        evil = os.path.normpath(os.path.join(server.WEB_DIR, "../api.py"))
        self.assertFalse(evil.startswith(base))


class TestRoundTrip(unittest.TestCase):
    """往返整合：一张往返票 / 两张单程 / 去回程分别拆段 的对比。"""

    @classmethod
    def setUpClass(cls):
        cls.q = api.make_query("北京", "纽约", "2026-12-10",
                               hubs=["IST", "DOH"], return_date="2026-12-18")
        cls.payload = api.run_roundtrip(cls.q, offline=True, max_hubs=2)

    def test_mode_and_ok(self):
        self.assertEqual(self.payload["mode"], "roundtrip")
        self.assertTrue(self.payload["ok"])

    def test_strategies_sorted_and_contain_key_options(self):
        strategies = self.payload["strategies"]
        self.assertTrue(strategies)
        keys = {s["key"] for s in strategies}
        self.assertIn("rt_economy", keys, "应当包含「一张往返票」")
        self.assertIn("two_oneway_economy", keys, "应当包含「两张单程票」")
        prices = [s["price"] for s in strategies]
        self.assertEqual(prices, sorted(prices), "策略应按总价升序")
        self.assertEqual(strategies[0]["delta_vs_cheapest"], 0)

    def test_has_direct_strategies(self):
        keys = {s["key"] for s in self.payload["strategies"]}
        direct = [k for k in keys if "direct" in k]
        self.assertTrue(direct, f"往返策略里缺少直飞选项，实际 keys={sorted(keys)}")

    def test_has_both_directions(self):
        self.assertTrue(self.payload["plans"], "缺去程方案")
        self.assertTrue(self.payload["ret_plans"], "缺回程方案")
        self.assertEqual(self.payload["query"]["return_date"], "2026-12-18")

    def test_json_serializable(self):
        json.dumps(self.payload, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)

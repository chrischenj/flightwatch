# 设计与接口契约（SPEC）

> 这份文档记录模块边界、数据契约和算法约定，方便二次开发、代码审查和贡献者快速上手。
> 代码注释里提到的「SPEC §x」都指向本文件的小节号。

## 1. 项目要解决什么

平台只卖「直飞 / 中转」整程票，且整程只有一个舱位。用户想：
1. 把长途拆成 **多段、分开买票**（每段可以不同航司、不同日期）；
2. 在「哪一段值得升公务舱好好睡一觉」上得到**量化建议**（多花的钱 / 多换回的休息小时）。

## 2. 目录与模块职责

```
flightwatch/
  models.py           数据模型（已冻结）
  airports.py         城市/枢纽/坐标（已冻结）
  api.py              集成层：查询 -> dict（给 Web/CLI 用）
  providers/
    base.py             Provider 接口
    kiwi.py             Kiwi 实时数据
    fixture.py          离线假数据（无网也能跑通全流程）
  engine/
    split.py            候选枢纽 -> 拆分方案
    score.py            休息价值评分
    optimize.py         组合、帕累托、推荐
  report.py           控制台 / HTML 报告
  cli.py              命令行
  server.py           stdlib http 服务 + JSON API 路由
  web/
    index.html          单页界面
    app.js              交互与渲染（零依赖）
    style.css           样式
tests/
  test_engine.py        引擎单测（纯逻辑，不联网）
  test_api.py           集成 / HTTP 层单测
```

## 3. 数据模型

见 `flightwatch/models.py`（已实现，直接用）：`Segment`、`Leg`、`LegChoice`、`SplitPlan`、
`Combo`、`Query`。舱位字符串：`ECONOMY / PREMIUM_ECONOMY / BUSINESS / FIRST_CLASS`。

## 4. engine 公共接口（api.py 只调用这几个函数）

```python
# engine/split.py
def candidate_hubs(q: Query) -> list[str]
def build_plan(q: Query, hub: str, fetch) -> SplitPlan | None
def build_plans(q: Query, fetch, hubs: list[str] | None = None, progress=None) -> list[SplitPlan]

# engine/score.py
def effective_rest_h(leg: Leg, cabin: str | None = None) -> float
def rest_score(leg: Leg, cabin: str | None = None) -> float          # 0..100
def cost_per_rest_hour(eco: Leg, biz: Leg) -> float | None

# engine/optimize.py
def build_combos(plan: SplitPlan) -> list[Combo]
def annotate(plan: SplitPlan) -> dict        # {"combos": [...], "best_value": {...}, "pareto": [...]}
def recommend(plans: list[SplitPlan], baseline: dict | None = None) -> dict
```

`fetch` 由 注入，签名固定：

```python
fetch(origin: str, dest: str, date: str, cabin: str) -> list[Leg]
# origin/dest 是两个城市码（如 "BJS"）；date 是 "YYYY-MM-DD"；
# 返回该日该舱位的候选票段（可能为空列表）。实现里有缓存和限速，可以放心多调。
```

## 5. 算法约定（可调参，但语义不要变）

### 5.1 候选枢纽（split.py）

- `q.hubs` 非空 → 用它；否则用 `airports.DEFAULT_HUBS`。
- 过滤：hub 不能等于 origin/destination 城市；两段各自直线距离 ≥ 300km；
  绕行比 `(d(o,h)+d(h,d)) / d(o,d) ≤ 1.6`（`d(o,d) < 500km` 时跳过绕行检查）。
- 排序：绕行比升序；默认最多取 **4** 个枢纽（`build_plans` 的 `hubs` 参数可覆盖）。
- 每段查询日期窗口：`date` 当天，以及 `date+1`（跨境长航线常见次日到达），
  取能拼成可行衔接的组合。

### 5.2 拆分方案（split.py::build_plan）

- 对每个 hub：查询 `(origin→hub)` 与 `(hub→dest)` 的 ECONOMY 和 BUSINESS（按需也要 PREMIUM_ECONOMY）。
- 衔接可行条件：`90min ≤ (leg2.depart - leg1.arrive) ≤ 20h`。
- 每种舱位取「价格最低且至少存在一个可行搭子」的票段，写入 `LegChoice.options[cabin]`。
- 某段完全没有 ECONOMY → 整个方案丢弃（经济舱是底价锚点）。
- `layovers_min` 取该方案经济舱组合的衔接时长。
- `note` 里写明「是否需要换机场 / 是否同一航站楼不可知 / 行李需重新托运」。

### 5.3 休息价值评分（score.py）

按**出发地当地时间**把「起飞后 30 分钟 ~ 落地前 45 分钟」的可用窗口，与夜间窗口
`[21:00, 07:00)` 求交集，得到 `sleep_h`。

```
duration_h   = leg.duration_min / 60
night_ratio  = sleep_h / max(duration_h, 0.1)          # 0..1
base_rest_h  = min(duration_h, sleep_h)                 # 真正能睡的小时
comfort      = {ECONOMY:0.35, PREMIUM_ECONOMY:0.60, BUSINESS:1.00, FIRST_CLASS:1.15}
effective_rest_h = base_rest_h * (0.15 + 0.85 * night_ratio) * comfort[cabin]
rest_score   = clamp(effective_rest_h / 8.0 * 100, 0, 100)

cost_per_rest_hour = (biz.price - eco.price) / max(rest_biz - rest_eco, 0.15)
```

要求：给定一样例可手算验证；纯白天短途分数低，跨夜长途商务舱接近满分。

### 5.4 组合与推荐（optimize.py）

- `build_combos`：每段可选舱位做笛卡尔积（只枚举该段实际有报价的舱位；至少经济舱）。
  总价 = Σ报价；总时长 = Σ飞行 + Σ衔接；`comfort_score = Σ effective_rest_h`。
  以「本方案全经济舱」为基准算 `price_delta` / `rest_gain` / `value_per_rest_hour`。
- `pareto`：在 (总价升序, rest_gain 降序) 上取非支配集合。
- `best_value`：只改一段舱位的组合里，`cost_per_rest_hour` 最小的那个，
  返回 `{"leg_index", "cabin", "extra_cost", "extra_rest_h", "cost_per_rest_h"}`。
- `recommend`：给出 `headline`（一句话结论）、`reasons`（≤4 条，必须含数字）、
  `combo`（推荐组合）、`warnings`（见 5.5）。没有可行方案时返回 `{"headline": "..."}`。

### 5.5 必须出现的风险提示（写进 recommend().warnings）

- 分段票不是联程票：航班延误不保护、行李通常不直挂、需自行重新托运安检。
- 需要核对中转国的过境签证要求（尤其美国/申根/英国）。
- 分段票若前段延误导致误机，后段按自愿弃程处理。
- 价格为公开接口实时枚举，可能存在缓存延迟，下单前必须复核。

## 6. `api.py` 输出的 JSON（前端按这个写）

```jsonc
{
  "ok": true,
  "query": {"origin":"BJS","origin_name":"北京","destination":"JFK","destination_name":"纽约",
            "date":"2026-12-10","date_window":1,"adults":1,"currency":"CNY",
            "hubs":["IST","DOH"],"max_ground_min":1200,"max_stops":1},
  "generated_at": "2026-10-25T22:00:00+08:00",
  "source": {"name":"Kiwi.com 公开接口","live": true},
  "baseline": {
    "through_economy": {"price": 5120.0, "duration_min": 1180, "stops": 1, "label":"整程经济舱"},
    "through_business": {"price": 21500.0, "duration_min": 1180, "stops": 1, "label":"整程公务舱"}
  },
  "plans": [{
    "hub": "IST", "hub_name": "伊斯坦布尔", "label": "北京 → 伊斯坦布尔 + 伊斯坦布尔 → 纽约",
    "note": "同一机场中转；行李需重新托运",
    "layovers_min": [180],
    "legs": [{
      "index": 0, "origin": "BJS", "destination": "IST",
      "distance_km": 7100.0, "tz_shift_h": -5.0,
      "options": {
        "ECONOMY": {"price":2172.0,"currency":"CNY","duration_min":925,"depart_local":"2026-12-10T02:45:00",
                    "arrive_local":"2026-12-10T08:10:00","cabin":"ECONOMY","stops":0,"carrier":"3U",
                    "booking_url":"https://...","segments":[{"carrier":"3U","flight_no":"3827","origin":"TFU",
                    "destination":"IST","depart_local":"...","arrive_local":"...","duration_min":560,"cabin":"ECONOMY"}]},
        "BUSINESS": {"price":10845.0, "...": "同上"}
      },
      "rest": {"score": 62.5, "effective_rest_h": {"ECONOMY":1.8,"BUSINESS":5.1},
               "sleep_h": 6.2, "night_ratio": 0.55, "verdict": "夜间长途，值得升舱"}
    }, {"index":1, "...": "..."}],
    "combos": [{"cabins":["ECONOMY","ECONOMY"],"cabins_cn":["经济舱","经济舱"],
                "total_price":4826.0,"currency":"CNY","total_duration_min":1830,
                "comfort_score":3.1,"price_delta":0.0,"rest_gain":0.0,
                "value_per_rest_hour":null,"legs":[]}],
    "best_value": {"leg_index":0,"cabin":"BUSINESS","extra_cost":8673.0,
                   "extra_rest_h":3.3,"cost_per_rest_h":2628.2},
    "pareto": [0, 2]
  }],
  "recommendation": {"headline":"...", "reasons":["..."], "combo":{...}, "warnings":["..."]},
  "warnings": ["..."],
  "errors": []
}
```

`legs[].rest` 的 `effective_rest_h` 是 `{舱位: 小时}`，`verdict` 是给人看的短句。

## 7. 运行方式

```bash
python -m flightwatch plan --from 北京 --to 纽约 --date 2026-12-10 --hubs IST,DOH --offline
python -m flightwatch serve            # http://127.0.0.1:8765
python -m pytest tests -q              # 或 python tests/test_engine.py
```

`--offline` 用 `providers/fixture.py` 的确定性假数据，不联网，便于开发与演示。

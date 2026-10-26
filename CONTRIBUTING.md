# 贡献指南

## 环境

- Python 3.9+
- **不需要任何第三方依赖**（刻意只用标准库）

```bash
git clone https://github.com/<your-name>/flightwatch.git
cd flightwatch
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -e .      # 可选，装完可以直接用 `flightwatch` 命令
```

## 跑测试

```bash
python tests/test_engine.py     # 引擎：评分 / 组合 / 推荐 / 护栏
python tests/test_api.py        # 集成：单程 / 往返 / 报告 / HTTP 路由
```

两个文件都用标准库 `unittest`，**不需要 pytest**，CI 会跑同样的命令
（见 `.github/workflows/ci.yml`，覆盖 Python 3.9–3.12 + Windows + macOS）。

## 代码约定

- 只依赖标准库。要加第三方依赖请先在 issue 里讨论。
- 中文注释，单位和口径写清楚（分钟 / 小时 / 元）。
- 数据源相关代码必须遵守：**低频、可缓存、失败可降级**。
  - 请求间隔 ≥ 0.3 秒（`providers/base.py::RateLimiter`）
  - 磁盘缓存默认 30 分钟（`DiskCache`）
  - 任何网络失败都要能返回空结果而不是抛到 UI
- 不要提交运行时产物：`data/cache/`、`data/history/`、`_preview/`、`__pycache__/`
  （`.gitignore` 已覆盖）。

## 想加一个新的数据源

实现 `flightwatch/providers/base.py` 里的 `Provider` 协议：

```python
def legs(origin, dest, date, cabin, max_stops=None) -> list[Leg]: ...
def return_trips(origin, dest, out_date, ret_date, cabin, max_stops=None) -> list[RoundTrip]: ...
```

然后在 `flightwatch/api.py::make_provider()` 里切换实现。核心算法（`engine/`）
完全不知道数据来自哪里，所以新数据源不需要改算法，也不需要改测试里的引擎用例。

## 提交 PR 前

1. 两个测试文件全绿；
2. 新功能尽量带单测（引擎逻辑不联网，很容易测）；
3. README / `docs/DESIGN.md` 如有行为变化请同步更新；
4. 一个 PR 只做一件事，描述里说清楚「为什么」而不只是「改了什么」。

## 报告安全问题

如果发现可能导致大量请求、绕过限速或泄露隐私的问题，请**先私下联系维护者**，
不要直接开公开 issue。

## 免责声明

本项目与 Kiwi.com 无隶属关系，使用的是其网页端公开端点。贡献者需理解并接受
[README 的免责声明](../README.md#免责声明disclaimers)。

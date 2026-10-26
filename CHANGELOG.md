# Changelog

All notable changes to this project are documented here.
格式参考 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循 [SemVer](https://semver.org/)。

## [0.1.0] — 2026-10-26

首个可下载版本 / first downloadable release.

### Added

- **拆段比价**：把长途按枢纽拆成 2 段，每段独立查经济舱 / 公务舱最低价，组合出 4 种混搭
- **升舱性价比评分**：`effective_rest = night_hours × (0.15 + 0.85 × night_ratio) × cabin_comfort`，
  用「每多睡 1 小时要花多少钱」排序，而不是只看差价
- **两道护栏**：升舱导致全程多 4 小时以上、或每多睡 1 小时超过 ¥4,000，结论直接说「不建议升舱」
- **直飞基准线**：每次查询额外用 `maxStopsCount=0` 查整程直飞，与最低中转、拆段方案并列对比
- **往返整合**：一张往返票 / 两张单程 / 去程拆 / 回程拆 / 双向都拆 / 直飞往返，按总价排序
- **实时数据源**：Kiwi.com 公开 GraphQL 端点，现场拉价；城市名也实时解析（不再依赖内置名单）
- **离线数据源**：确定性的合成数据，无网络也能演示与跑测试
- **四种用法**：本地网页界面、命令行、HTML 报告导出、`watch` 定时价格监控
- **网页界面**：零 CDN、纯 HTML/CSS/JS，异步查询 + 实时进度，失败明确报错而非静默降级
- **零第三方依赖**：仅需 Python 3.9+ 标准库
- **测试**：33 个引擎用例 + 20 个集成用例（标准库 `unittest`，不需要 pytest）
- **CI**：GitHub Actions，覆盖 Python 3.9–3.12 + Windows + macOS

### Notes

- 数据来自 Kiwi.com 网页端使用的公开端点，**非官方 API**，不保证稳定；
  本项目与其无隶属关系，请低频自用。详见 [DISCLAIMER.md](DISCLAIMER.md)。
- 价格为搜索枚举价而非报价承诺，下单前请以航司 / OTA 页面为准。

[0.1.0]: https://github.com/chrischenj/flightwatch/releases/tag/v0.1.0

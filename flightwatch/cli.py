"""命令行入口。

    python -m flightwatch plan --from 北京 --to 纽约 --date 2026-12-10 --hubs IST,DOH
    python -m flightwatch plan --from 北京 --to 纽约 --date 2026-12-10 --offline
    python -m flightwatch serve --port 8765
    python -m flightwatch export --from 北京 --to 纽约 --date 2026-12-10 -o 报告.html
"""
from __future__ import annotations

import argparse
import os
import sys

from . import api, report


def _split_hubs(raw: str) -> list[str]:
    if not raw:
        return []
    return [h.strip() for h in raw.replace("，", ",").split(",") if h.strip()]


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--from", dest="origin", required=True, help="出发城市：中文名 / 英文名 / 三字码")
    p.add_argument("--to", dest="destination", required=True, help="到达城市")
    p.add_argument("--date", required=True, help="出发日期 YYYY-MM-DD")
    p.add_argument("--hubs", default="", help="候选拆分枢纽，逗号分隔（留空自动选）")
    p.add_argument("--window", type=int, default=0, help="出发日期前后浮动天数（0-2）")
    p.add_argument("--currency", default="CNY")
    p.add_argument("--max-hubs", type=int, default=api.DEFAULT_MAX_HUBS, help="最多评估几个枢纽")
    p.add_argument("--offline", action="store_true", help="用离线示例数据（不联网）")
    p.add_argument("--refresh", action="store_true", help="跳过本地价格缓存，强制取最新价")
    p.add_argument("--return", dest="return_date", default="",
                   help="返程日期 YYYY-MM-DD；填了就做「往返整合」对比")


def _run(q, args, progress=None):
    """有返程日期就走往返整合，否则单程。"""
    common = dict(offline=args.offline, max_hubs=args.max_hubs,
                  refresh=getattr(args, "refresh", False))
    if getattr(args, "return_date", ""):
        return api.run_roundtrip(q, progress=progress, **common)
    return api.run(q, progress=progress, **common)


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        q = api.make_query(
            args.origin, args.destination, args.date,
            date_window=args.window, currency=args.currency, hubs=_split_hubs(args.hubs),
            return_date=getattr(args, "return_date", ""),
        )
    except ValueError as e:
        print(f"参数错误：{e}", file=sys.stderr)
        return 2

    def progress(done, total, label):
        if total:
            print(f"\r  {label} {done}/{total} ...", end="", file=sys.stderr, flush=True)

    payload = _run(q, args, progress=progress)
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
    print(report.render_console(payload))
    return 0 if payload.get("ok") else 1


def cmd_export(args: argparse.Namespace) -> int:
    try:
        q = api.make_query(
            args.origin, args.destination, args.date,
            date_window=args.window, currency=args.currency, hubs=_split_hubs(args.hubs),
            return_date=getattr(args, "return_date", ""),
        )
    except ValueError as e:
        print(f"参数错误：{e}", file=sys.stderr)
        return 2
    payload = _run(q, args)
    out = args.output or os.path.join(os.getcwd(), "分舱段分析报告.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(report.render_html(payload))
    print(f"已写出报告：{out}")
    return 0 if payload.get("ok") else 1


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def _snapshot(payload: dict) -> dict:
    """从一次结果里抽出可用于比价的历史标量。"""
    snap = {"ts": payload.get("generated_at"), "plans": {}}
    for plan in payload.get("plans") or []:
        key = plan.get("hub")
        legs = {}
        for leg in plan.get("legs") or []:
            opts = leg.get("options") or {}
            legs[str(leg.get("index"))] = {
                "route": f"{leg['origin']}-{leg['destination']}",
                "economy": (opts.get("ECONOMY") or {}).get("price"),
                "business": (opts.get("BUSINESS") or {}).get("price"),
                "cost_per_rest_h": (leg.get("rest") or {}).get("cost_per_rest_h"),
            }
        snap["plans"][key] = {
            "legs": legs,
            "best_value": plan.get("best_value"),
            "cheapest_total": (plan.get("combos") or [{}])[0].get("total_price"),
        }
    return snap


def _diff_snapshots(old: dict, new: dict) -> list[str]:
    msgs = []
    for hub, plan in (new.get("plans") or {}).items():
        prev = (old.get("plans") or {}).get(hub) or {}
        for idx, leg in (plan.get("legs") or {}).items():
            pleg = (prev.get("legs") or {}).get(idx) or {}
            for cabin in ("economy", "business"):
                a, b = pleg.get(cabin), leg.get(cabin)
                if a and b and abs(b - a) >= 30:
                    arrow = "↓" if b < a else "↑"
                    msgs.append(f"  {hub} 第{int(idx) + 1}段 {cabin} {a:,.0f} → {b:,.0f} {arrow}")
        bv_new = plan.get("best_value") or {}
        bv_old = prev.get("best_value") or {}
        if bv_new.get("cost_per_rest_h") and bv_old.get("cost_per_rest_h"):
            r = bv_new["cost_per_rest_h"] / bv_old["cost_per_rest_h"]
            if r <= 0.9:
                msgs.append(
                    f"  {hub} 升舱性价比变好：每多睡 1 小时 "
                    f"{bv_old['cost_per_rest_h']:,.0f} → {bv_new['cost_per_rest_h']:,.0f}"
                )
    return msgs


def cmd_watch(args: argparse.Namespace) -> int:
    """按固定间隔重复查询，把结果存成快照并报告价格变化。"""
    import json
    import time as _time

    out_dir = args.out or os.path.join(os.getcwd(), "data", "history")
    os.makedirs(out_dir, exist_ok=True)
    history: list[dict] = []
    rounds = args.rounds if args.rounds > 0 else 10 ** 9
    last_snap = None
    for i in range(rounds):
        try:
            q = api.make_query(
                args.origin, args.destination, args.date,
                date_window=args.window, currency=args.currency, hubs=_split_hubs(args.hubs),
                return_date=getattr(args, "return_date", ""),
            )
            payload = _run(q, args)
        except Exception as e:  # noqa: BLE001
            print(f"[{_time.strftime('%H:%M:%S')}] 本轮失败：{type(e).__name__}: {e}", file=sys.stderr)
            payload = None

        if payload:
            snap = _snapshot(payload)
            stamp = _time.strftime("%Y%m%d-%H%M%S")
            with open(os.path.join(out_dir, f"snap-{stamp}.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            history.append(snap)
            with open(os.path.join(out_dir, "index.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(snap, ensure_ascii=False) + "\n")
            print(f"\n[{_time.strftime('%H:%M:%S')}] 第 {i + 1} 轮")
            print(report.render_console(payload))
            if last_snap:
                changes = _diff_snapshots(last_snap, snap)
                print("\n价格变化：" + ("无显著变化（阈值 ¥30）" if not changes else ""))
                for m in changes:
                    print(m)
                cheapest = min(
                    (p.get("cheapest_total") for p in snap["plans"].values() if p.get("cheapest_total")),
                    default=None,
                )
                if cheapest and args.alert_below and cheapest < args.alert_below:
                    print(f"\n*** 提醒：全经济舱最低总价 ¥{cheapest:,.0f}，已低于阈值 ¥{args.alert_below:,} ***")
            last_snap = snap

        if i + 1 < rounds:
            print(f"\n等待 {args.interval} 分钟后复查……（Ctrl+C 停止）")
            try:
                _time.sleep(args.interval * 60)
            except KeyboardInterrupt:
                break
    print(f"\n快照已保存到 {out_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flightwatch", description="分舱段购票 + 升舱建议助手")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="在终端里算一次（含结论）")
    _add_common(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_exp = sub.add_parser("export", help="导出可归档的 HTML 报告")
    _add_common(p_exp)
    p_exp.add_argument("-o", "--output", default="")
    p_exp.set_defaults(func=cmd_export)

    p_watch = sub.add_parser("watch", help="定时复查价格并记录快照（简单监控）")
    _add_common(p_watch)
    p_watch.add_argument("--interval", type=int, default=30, help="复查间隔（分钟）")
    p_watch.add_argument("--rounds", type=int, default=0, help="复查轮数（0=一直跑）")
    p_watch.add_argument("--out", default="", help="快照目录")
    p_watch.add_argument("--alert-below", type=float, default=0, help="全经济舱总价低于该值时提醒")
    p_watch.set_defaults(func=cmd_watch)

    p_web = sub.add_parser("serve", help="启动本地 Web 界面")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8765)
    p_web.add_argument("--no-browser", action="store_true")
    p_web.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

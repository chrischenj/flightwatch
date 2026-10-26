"""把分析结果渲染成「看得懂」的形式：终端表格 + 可归档的 HTML 报告。"""
from __future__ import annotations

import html
import time

from .airports import city_name
from .models import CABIN_CN


def airports_cn(code) -> str:
    """城市码 -> 中文名（报告里给用户看）。"""
    return city_name(str(code)) if code else ""


def _money(v) -> str:
    if v is None:
        return "—"
    return f"¥{v:,.0f}"


def _rt_strategy_html(payload: dict) -> str:
    """往返模式：购买方式对比（一张往返票 vs 两张单程 vs 分段）。"""
    strategies = payload.get("strategies") or []
    rec = payload.get("recommendation") or {}
    out = ["<div class='card'><h2>购买方式对比（价格低的在前）</h2>"]
    out.append("<table><tr><th>购买方式</th><th class='num'>总价</th><th class='num'>比最省多花</th>"
               "<th class='num'>全程时长</th><th>说明</th></tr>")
    best = strategies[0] if strategies else None
    for s in strategies:
        mark = " 🔸最省" if best and s.get("key") == best.get("key") else ""
        color = "eco" if s.get("cabin") == "ECONOMY" else ("biz" if s.get("cabin") == "BUSINESS" else "")
        out.append(
            f"<tr><td class='{color}'>{_e(s.get('label'))}{mark}</td>"
            f"<td class='num'>¥{float(s.get('price') or 0):,.0f}</td>"
            f"<td class='num'>{_money(s.get('delta_vs_cheapest'))}</td>"
            f"<td class='num'>{_hm(s.get('duration_min'))}</td>"
            f"<td class='muted'>{_e(s.get('note') or s.get('seller') or '')}</td></tr>"
        )
    out.append("</table>")
    if rec.get("headline"):
        out.append(f"<p class='good'>{_e(rec['headline'])}</p>")
    out.append("</div>")
    return "".join(out)


def _render_console_rt(payload: dict) -> str:
    """往返模式的终端输出。"""
    lines: list[str] = []
    q = payload.get("query", {})
    lines.append("=" * 78)
    lines.append(
        f"  {q.get('origin_name', '')}({q.get('origin', '')}) ⇄ "
        f"{q.get('destination_name', '')}({q.get('destination', '')})"
        f"   去 {q.get('date', '')} / 回 {q.get('return_date', '')}"
    )
    lines.append(f"  数据源：{payload.get('source', {}).get('name', '?')}")
    lines.append("=" * 78)
    if payload.get("price_note"):
        lines.append(f"  {payload['price_note']}")
        lines.append("=" * 78)

    strategies = payload.get("strategies") or []
    lines.append("购买方式对比（便宜的在前）：")
    best_key = strategies[0]["key"] if strategies else None
    for i, s in enumerate(strategies, 1):
        mark = "  ← 最省" if s.get("key") == best_key else f"  (+{_money(s.get('delta_vs_cheapest'))})"
        lines.append(
            f"  {i}. {s.get('label', ''):<36s} {_money(s.get('price')):>10s}   "
            f"{_hm(s.get('duration_min')):>12s}{mark}"
        )
        if s.get("note"):
            lines.append(f"       {s['note']}")

    rec = payload.get("recommendation") or {}
    if rec.get("headline"):
        lines.append("")
        lines.append(f"结论：{rec['headline']}")
        for r in rec.get("reasons") or []:
            lines.append(f"  · {r}")

    for direction, key in (("去程", "plans"), ("回程", "ret_plans")):
        plans = payload.get(key) or []
        if not plans:
            continue
        lines.append("")
        lines.append("=" * 78)
        lines.append(f"  {direction}拆分明细")
        lines.append("=" * 78)
        sub = {
            "query": payload.get("query", {}),
            "baseline": {},
            "plans": plans,
            "recommendation": {},
            "warnings": [],
            "errors": [],
        }
        lines.append(render_console(sub))

    warns = payload.get("warnings") or []
    if warns:
        lines.append("")
        lines.append("风险提示：")
        for w in warns:
            lines.append(f"  ! {w}")
    errs = payload.get("errors") or []
    if errs:
        lines.append("")
        lines.append("接口/数据问题：")
        for e in errs[:6]:
            lines.append(f"  x {e}")
    return "\n".join(lines)


def _hm(minutes) -> str:
    if not minutes:
        return "—"
    m = int(minutes)
    return f"{m // 60}小时{m % 60:02d}分"


def _cabins(cabins) -> str:
    return " + ".join(CABIN_CN.get(c, c) for c in cabins)


def render_console(payload: dict) -> str:
    if payload.get("mode") == "roundtrip":
        return _render_console_rt(payload)
    lines: list[str] = []
    q = payload.get("query", {})
    lines.append("=" * 78)
    lines.append(
        f"  {q.get('origin_name', '')}({q.get('origin', '')}) → "
        f"{q.get('destination_name', '')}({q.get('destination', '')})   {q.get('date', '')}"
        f"   数据源：{payload.get('source', {}).get('name', '?')}"
    )
    lines.append("=" * 78)

    if payload.get("price_note"):
        lines.append(f"  {payload['price_note']}")
        lines.append("=" * 78)

    base = payload.get("baseline") or {}
    te, tb = base.get("through_economy"), base.get("through_business")
    ne, nb = base.get("nonstop_economy"), base.get("nonstop_business")
    if te:
        lines.append(
            f"整程直搜最低：经济舱 {_money(te['price'])} / {_hm(te['duration_min'])} / "
            f"{te['stops']}次中转"
        )
    if tb:
        lines.append(
            f"              公务舱 {_money(tb['price'])} / {_hm(tb['duration_min'])} / "
            f"{tb['stops']}次中转"
        )
    if ne:
        lines.append(
            f"整程直飞：    经济舱 {_money(ne['price'])} / {_hm(ne['duration_min'])} / 0次中转"
        )
    if nb:
        lines.append(
            f"              公务舱 {_money(nb['price'])} / {_hm(nb['duration_min'])} / 0次中转"
        )
    if payload.get("direct_note"):
        lines.append(f"  → {payload['direct_note']}")

    for plan in payload.get("plans") or []:
        lines.append("")
        lines.append("-" * 78)
        lines.append(f"【在 {plan.get('hub_name', plan['hub'])} 拆票】{plan.get('label', '')}")
        if plan.get("note"):
            lines.append(f"  备注：{plan['note']}")
        for leg in plan.get("legs") or []:
            opts = leg.get("options") or {}
            eco, biz = opts.get("ECONOMY"), opts.get("BUSINESS")
            rest = leg.get("rest") or {}
            lines.append(
                f"  第{leg['index'] + 1}段 {leg['origin']}→{leg['destination']}"
                f"（{leg.get('distance_km', 0):,.0f}km "
                f"{(leg.get('tz_shift_h') or 0):+.0f}h时差）"
            )
            if eco:
                lines.append(
                    f"      经济舱 {_money(eco['price'])}  {_hm(eco['duration_min'])}  "
                    f"{eco['depart_local'][5:16]} → {eco['arrive_local'][5:16]}  "
                    f"{eco.get('baggage_note', '')}"
                )
            if biz:
                lines.append(
                    f"      公务舱 {_money(biz['price'])}  {_hm(biz['duration_min'])}  "
                    f"{biz['depart_local'][5:16]} → {biz['arrive_local'][5:16]}  "
                    f"{biz.get('baggage_note', '')}"
                )
            if eco and biz:
                lines.append(
                    f"      升舱多花 {_money(biz['price'] - eco['price'])}；"
                    f"夜间可睡 {rest.get('sleep_h', 0):.1f}h；"
                    f"每多睡 1 小时成本 {_money(rest.get('cost_per_rest_h'))}；{rest.get('verdict', '')}"
                )
                if rest.get("upgrade_note"):
                    lines.append(f"      {rest['upgrade_note']}")
        combos = plan.get("combos") or []
        if combos:
            lines.append("  组合对比（总价 / 全程时长 / 相当于多睡）：")
            for c in combos:
                lines.append(
                    f"    {_cabins(c['cabins']):<12} {_money(c['total_price']):>10}   "
                    f"{_hm(c['total_duration_min']):>12}   休息收益 {c.get('rest_gain', 0):+.1f}h"
                    f"   比全经济多花 {_money(c.get('price_delta'))}"
                )
        bv = plan.get("best_value")
        if bv:
            lines.append(
                f"  → 这一段最值得升舱：第{bv['leg_index'] + 1}段，"
                f"多花 {_money(bv['extra_cost'])} 换 {bv.get('extra_rest_h', 0):.1f} 小时休息"
                f"（{_money(bv.get('cost_per_rest_h'))}/小时）"
            )

    rec = payload.get("recommendation") or {}
    if rec.get("headline"):
        lines.append("")
        lines.append("=" * 78)
        lines.append(f"结论：{rec['headline']}")
        for r in rec.get("reasons") or []:
            lines.append(f"  · {r}")
        combo = rec.get("combo")
        if combo:
            lines.append(
                f"  推荐组合：{_cabins(combo.get('cabins', []))} 总价 {_money(combo.get('total_price'))}"
                f"（比全经济多 {_money(combo.get('price_delta'))}，多睡 "
                f"{combo.get('rest_gain', 0):.1f}h）"
            )
        forced = rec.get("upgrade_if_forced")
        if forced:
            lines.append(
                f"  如果一定要升一段：{airports_cn(forced.get('plan_hub'))} 的第 "
                f"{int(forced.get('leg_index', 0)) + 1} 段，多花 {_money(forced.get('extra_cost'))} "
                f"换 {forced.get('extra_rest_h', 0):.1f}h（{_money(forced.get('cost_per_rest_h'))}/小时）"
            )

    warns = payload.get("warnings") or []
    if warns:
        lines.append("")
        lines.append("风险提示：")
        for w in warns:
            lines.append(f"  ! {w}")
    errs = payload.get("errors") or []
    if errs:
        lines.append("")
        lines.append("接口/数据问题：")
        for e in errs[:6]:
            lines.append(f"  x {e}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- HTML
_CSS = """
:root{--bg:#0f1420;--card:#18202f;--line:#26304a;--fg:#e8edf7;--muted:#8b9ab5;
--eco:#4da3ff;--biz:#ffb347;--good:#43d19e;--warn:#ff7b7b}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.6}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:28px 0 10px;color:#cdd8ee}
.sub{color:var(--muted);font-size:13px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:14px 0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--muted);font-weight:500}
.num{text-align:right;font-variant-numeric:tabular-nums}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;background:#22304a;color:#a9c0e8}
.eco{color:var(--eco)} .biz{color:var(--biz)} .good{color:var(--good)}
.rec{border-left:4px solid var(--good)}
.warn{color:var(--warn);font-size:13px}
.bar{height:8px;border-radius:4px;background:#22304a;overflow:hidden;min-width:80px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#4da3ff,#43d19e)}
.muted{color:var(--muted);font-size:12px}
"""


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def render_html(payload: dict) -> str:
    q = payload.get("query", {})
    title = f"{q.get('origin_name', '')} → {q.get('destination_name', '')} {q.get('date', '')}"
    if q.get("return_date"):
        title = (f"{q.get('origin_name', '')} ⇄ {q.get('destination_name', '')} "
                 f"{q.get('date', '')} 去 / {q.get('return_date', '')} 回")
    parts: list[str] = []
    parts.append(
        f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_e(title)} · 分舱段购票分析</title><style>{_CSS}</style></head><body>"
    )
    parts.append(f"<h1>{_e(title)}</h1>")
    parts.append(
        f"<div class='sub'>数据源：{_e(payload.get('source', {}).get('name'))} · "
        f"生成于 {_e(payload.get('generated_at'))} · 耗时 {_e(payload.get('elapsed_sec'))}s</div>"
    )
    if payload.get("price_note"):
        parts.append(f"<div class='card' style='padding:12px 16px'>{_e(payload['price_note'])}</div>")

    if payload.get("mode") == "roundtrip":
        parts.append(_rt_strategy_html(payload))

    base = payload.get("baseline") or {}
    te, tb = base.get("through_economy"), base.get("through_business")
    ne, nb = base.get("nonstop_economy"), base.get("nonstop_business")
    if te or tb or ne or nb:
        parts.append("<div class='card'><h2>整程基准价：直飞 vs 最低中转</h2>"
                     "<table><tr><th>类型</th><th>舱位</th><th class='num'>价格</th>"
                     "<th class='num'>时长</th><th class='num'>中转</th></tr>")
        for label, b, cls in (
            ("最低中转", te, "eco"), ("最低中转", tb, "biz"),
            ("直飞", ne, "eco"), ("直飞", nb, "biz"),
        ):
            if b:
                name = "经济舱" if cls == "eco" else "公务舱"
                parts.append(
                    f"<tr><td>{label}</td><td class='{cls}'>{name}</td>"
                    f"<td class='num'>¥{b['price']:,.0f}</td>"
                    f"<td class='num'>{_hm(b['duration_min'])}</td><td class='num'>{b['stops']} 次</td></tr>"
                )
        parts.append("</table>")
        if payload.get("direct_note"):
            parts.append(f"<p class='muted'>{_e(payload['direct_note'])}</p>")
        parts.append("</div>")

    groups = [("去程", payload.get("plans") or [])]
    if payload.get("ret_plans"):
        groups.append(("回程", payload.get("ret_plans") or []))
    for dirn, plan in [(d, p) for d, ps in groups for p in ps]:
        parts.append("<div class='card'>")
        parts.append(f"<h2>{dirn} · 在 {_e(plan.get('hub_name', plan['hub']))} 拆票</h2>")
        if plan.get("note"):
            parts.append(f"<div class='muted'>{_e(plan['note'])}</div>")
        parts.append("<table><tr><th>段</th><th>航线</th><th class='num'>经济舱</th>"
                     "<th class='num'>公务舱</th><th class='num'>升舱差价</th>"
                     "<th class='num'>夜间可睡</th><th class='num'>每多睡1h成本</th><th>判断</th></tr>")
        for leg in plan.get("legs") or []:
            opts = leg.get("options") or {}
            eco, biz = opts.get("ECONOMY"), opts.get("BUSINESS")
            rest = leg.get("rest") or {}
            diff = (biz["price"] - eco["price"]) if (eco and biz) else None
            parts.append(
                f"<tr><td>{leg['index'] + 1}</td>"
                f"<td>{_e(leg['origin'])} → {_e(leg['destination'])}"
                f"<div class='muted'>{_e((eco or biz or {}).get('depart_local', '')[5:16])} → "
                f"{_e((eco or biz or {}).get('arrive_local', '')[5:16])} · "
                f"{_hm((eco or biz or {}).get('duration_min'))}</div></td>"
                f"<td class='num eco'>{_money(eco['price']) if eco else '—'}</td>"
                f"<td class='num biz'>{_money(biz['price']) if biz else '—'}</td>"
                f"<td class='num'>{_money(diff)}</td>"
                f"<td class='num'>{rest.get('sleep_h', 0):.1f}h</td>"
                f"<td class='num'>{_money(rest.get('cost_per_rest_h'))}</td>"
                f"<td class='muted'>{_e(rest.get('verdict'))}</td></tr>"
            )
        parts.append("</table>")

        combos = plan.get("combos") or []
        if combos:
            max_price = max(c["total_price"] for c in combos) or 1
            parts.append("<h2>组合对比</h2><table><tr><th>组合</th><th class='num'>总价</th>"
                         "<th class='num'>比全经济多花</th><th class='num'>休息收益</th>"
                         "<th class='num'>每多睡1h成本</th><th>性价比</th></tr>")
            for c in combos:
                ratio = (c["total_price"] / max_price) * 100
                parts.append(
                    f"<tr><td>{_e(_cabins(c['cabins']))}</td>"
                    f"<td class='num'>¥{c['total_price']:,.0f}</td>"
                    f"<td class='num'>{_money(c.get('price_delta'))}</td>"
                    f"<td class='num good'>{c.get('rest_gain', 0):+.1f}h</td>"
                    f"<td class='num'>{_money(c.get('value_per_rest_hour'))}</td>"
                    f"<td><div class='bar'><i style='width:{ratio:.0f}%'></i></div></td></tr>"
                )
            parts.append("</table>")
        bv = plan.get("best_value")
        if bv:
            parts.append(
                f"<p class='good'>该方案最值得升舱：第 {bv['leg_index'] + 1} 段，"
                f"多花 ¥{bv['extra_cost']:,.0f} 换 {bv.get('extra_rest_h', 0):.1f} 小时休息"
                f"（¥{bv.get('cost_per_rest_h', 0):,.0f}/小时）</p>"
            )
        parts.append("</div>")

    rec = payload.get("recommendation") or {}
    if rec.get("headline"):
        parts.append("<div class='card rec'><h2>结论</h2>")
        parts.append(f"<p style='font-size:16px'>{_e(rec['headline'])}</p>")
        if rec.get("reasons"):
            parts.append("<ul>" + "".join(f"<li>{_e(r)}</li>" for r in rec["reasons"]) + "</ul>")
        combo = rec.get("combo")
        if combo:
            parts.append(
                f"<p>推荐组合：<b>{_e(_cabins(combo.get('cabins', [])))}</b>，"
                f"总价 <b>¥{combo.get('total_price', 0):,.0f}</b>，"
                f"比全经济多 ¥{combo.get('price_delta', 0):,.0f}，多睡 {combo.get('rest_gain', 0):.1f} 小时。</p>"
            )
        parts.append("</div>")

    warns = payload.get("warnings") or []
    if warns:
        parts.append("<div class='card'><h2>风险提示</h2>")
        parts.append("".join(f"<div class='warn'>! {_e(w)}</div>" for w in warns))
        parts.append("</div>")

    parts.append(f"<div class='muted'>生成时间 {time.strftime('%Y-%m-%d %H:%M:%S')} · 仅供比价参考</div>")
    parts.append("</body></html>")
    return "".join(parts)

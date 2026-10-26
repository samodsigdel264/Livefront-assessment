"""
Build a single self-contained HTML report (inline SVG, no external requests) from the gold layer.

    python scripts/build_report.py                         # -> reports/index.html
    python scripts/build_report.py --dq-db warehouse/dirty_demo.duckdb   # adds the "what quality checks caught" card

Every chart title states the finding, computed from the data, so the report cannot drift from the tables.
"""

from __future__ import annotations

import argparse
import html
from datetime import date
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent

CSS = """
:root{color-scheme:light;--bg:#f6f6f4;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#8a8984;--grid:#e6e5e1;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--line:#dcdbd6}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--bg:#121211;--surface:#1a1a19;--ink:#f0efec;
--ink2:#c3c2b7;--muted:#8f8e86;--grid:#2c2c2a;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--line:#33332f}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#121211;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#8f8e86;--grid:#2c2c2a;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--line:#33332f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1000px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:28px;margin:0 0 4px;letter-spacing:-.02em}.sub{color:var(--ink2);margin:0 0 24px;max-width:70ch}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi b{display:block;font-size:26px;letter-spacing:-.02em;font-variant-numeric:tabular-nums}.kpi span{color:var(--ink2);font-size:13px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:20px 20px 14px;margin-bottom:16px}
.card h2{font-size:17px;margin:0 0 2px;letter-spacing:-.01em}.card p.note{color:var(--ink2);font-size:13px;margin:2px 0 12px;max-width:75ch}
.card .src{color:var(--muted);font-size:12px;margin:8px 0 0}
svg{width:100%;height:auto;display:block;overflow:visible}svg text{fill:var(--ink2);font-size:11px;font-family:inherit}
svg .ink{fill:var(--ink)}svg .grid{stroke:var(--grid);stroke-width:1}svg .axis{stroke:var(--line)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--ink2);margin:0 0 6px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.col .xh{stroke:var(--ink2);stroke-width:1;opacity:0}.col:hover .xh{opacity:.6}.hit{fill:transparent}
[data-tip]:hover{stroke:var(--ink);stroke-width:1.5}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--bg);padding:6px 9px;border-radius:6px;font-size:12px;
line-height:1.4;opacity:0;z-index:9;max-width:260px;white-space:pre-line}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}details{margin-top:8px;font-size:13px;color:var(--ink2)}
summary{cursor:pointer}.warn{border-left:3px solid var(--s2);padding-left:10px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:760px){.two{grid-template-columns:1fr}}
"""

JS = """
const tip=document.getElementById('tip');
document.addEventListener('mousemove',e=>{const t=e.target.closest('[data-tip]');
 if(!t){tip.style.opacity=0;return}
 tip.textContent=t.getAttribute('data-tip');tip.style.opacity=1;
 const x=Math.min(e.clientX+14,innerWidth-tip.offsetWidth-8);tip.style.left=x+'px';tip.style.top=(e.clientY+14)+'px'});
"""


def esc(s):
    return html.escape(str(s), quote=True)


def fmt_int(n):
    return f"{int(n):,}"


def usd(n, d=0):
    return f"${float(n):,.{d}f}"


def pct(x, d=0):
    return f"{100 * float(x):.{d}f}%"


def nice_max(v):
    if v <= 0:
        return 1
    import math
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * e:
            return m * e
    return 10 * e


def card(title, note, body, src="", extra=""):
    return (f'<section class="card"><h2>{esc(title)}</h2><p class="note">{note}</p>{body}{extra}'
            f'{f"<p class=src>{esc(src)}</p>" if src else ""}</section>')


def legend(items):
    return '<div class="legend">' + "".join(
        f'<span><i style="background:var(--{c})"></i>{esc(n)}</span>' for n, c in items) + "</div>"


# ----------------------------------------------------------------------------- charts

def line_chart(dates, series, ymax=None, w=900, h=260, shade_until=None):
    """series: [(name, css-var, values)]. One axis, direct end labels, per-column hover."""
    ml, mr, mt, mb = 40, 70, 10, 24
    n = len(dates)
    ymax = nice_max(ymax or max(max(v) for _, _, v in series))
    X = lambda i: ml + (w - ml - mr) * i / max(n - 1, 1)
    Y = lambda v: mt + (h - mt - mb) * (1 - v / ymax)
    out = [f'<svg viewBox="0 0 {w} {h}" role="img">']
    for k in range(5):
        v = ymax * k / 4
        out.append(f'<line class="grid" x1="{ml}" x2="{w - mr}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>'
                   f'<text x="{ml - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{v:g}</text>')
    if shade_until:
        out.append(f'<rect x="{ml}" y="{mt}" width="{X(shade_until) - ml:.1f}" height="{h - mt - mb}" fill="var(--grid)" opacity=".45"/>'
                   f'<text x="{ml + 6}" y="{mt + 12}">warm-up (trailing windows truncated)</text>')
    last_month = None
    for i, d in enumerate(dates):
        if d.month != last_month:
            last_month = d.month
            out.append(f'<text x="{X(i):.1f}" y="{h - 6}" text-anchor="start">{d.strftime("%b")}</text>')
    for name, c, vals in series:
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
        out.append(f'<polyline fill="none" stroke="var(--{c})" stroke-width="2" stroke-linejoin="round" points="{pts}"/>'
                   f'<text x="{w - mr + 6}" y="{Y(vals[-1]) + 4:.1f}" style="fill:var(--ink)">{esc(name)} {vals[-1]:g}</text>')
    cw = (w - ml - mr) / max(n - 1, 1)
    for i, d in enumerate(dates):
        tip = d.strftime("%a %b %d") + "".join(f"\n{nm}: {v[i]:g}" for nm, _, v in series)
        out.append(f'<g class="col"><line class="xh" x1="{X(i):.1f}" x2="{X(i):.1f}" y1="{mt}" y2="{h - mb}"/>'
                   f'<rect class="hit" x="{X(i) - cw / 2:.1f}" y="{mt}" width="{cw:.1f}" height="{h - mt - mb}" data-tip="{esc(tip)}"/></g>')
    out.append("</svg>")
    return "".join(out)


def retention_heatmap(rows, w=900):
    """rows: [(cohort_date, size, {period: rate})]."""
    ml, mt = 78, 40
    cw, ch = 28, 15
    maxp = max(max(r[2]) for r in rows)
    h = mt + ch * len(rows) + 8
    out = [f'<svg viewBox="0 0 {ml + cw * (maxp + 1) + 10} {h}" role="img">']
    for p in range(0, maxp + 1, 2):
        out.append(f'<text x="{ml + cw * p + cw / 2}" y="{mt - 6}" text-anchor="middle">{p}</text>')
    out.append(f'<text x="{ml}" y="9">weeks since signup week →</text>')
    for r, (d, size, rates) in enumerate(rows):
        y = mt + r * ch
        out.append(f'<text x="{ml - 8}" y="{y + 11}" text-anchor="end">{d.strftime("%b %d")} · n={size}</text>')
        for p, rate in sorted(rates.items()):
            tip = f"Cohort of {d.strftime('%b %d')} (n={size})\nweek {p}: {pct(rate, 1)} bought"
            out.append(f'<rect x="{ml + p * cw + 1}" y="{y + 1}" width="{cw - 2}" height="{ch - 2}" rx="2" fill="var(--s1)" '
                       f'fill-opacity="{max(0.06, min(rate / 0.9, 1)):.2f}" data-tip="{esc(tip)}"/>')
    out.append("</svg>")
    return "".join(out)


def hbar_stacked(rows, series, w=900, label_w=220):
    """rows: [(label, [v1, v2...])]; series: [(name, css-var)]. 2px gap between segments."""
    bh, gap, mt = 20, 8, 4
    h = mt + len(rows) * (bh + gap)
    mx = nice_max(max(sum(v) for _, v in rows))
    scale = (w - label_w - 90) / mx
    out = [f'<svg viewBox="0 0 {w} {h}" role="img">']
    for r, (lab, vals) in enumerate(rows):
        y = mt + r * (bh + gap)
        out.append(f'<text x="{label_w - 8}" y="{y + 14}" text-anchor="end" class="ink">{esc(lab)}</text>')
        x = label_w
        for (nm, c), v in zip(series, vals):
            wd = v * scale
            if wd > 0:
                out.append(f'<rect x="{x:.1f}" y="{y}" width="{max(wd - 2, 1):.1f}" height="{bh}" rx="3" fill="var(--{c})" '
                           f'data-tip="{esc(lab)}\n{esc(nm)}: {usd(v, 2)}"/>')
            x += wd
        out.append(f'<text x="{x + 4:.1f}" y="{y + 14}">{usd(sum(vals))}</text>')
    out.append("</svg>")
    return "".join(out)


def funnel(stages, w=900):
    bh, gap, label_w = 30, 10, 220
    top = stages[0][1]
    h = len(stages) * (bh + gap)
    out = [f'<svg viewBox="0 0 {w} {h}" role="img">']
    for i, (name, n, pct_prev) in enumerate(stages):
        y = i * (bh + gap)
        wd = (w - label_w - 190) * n / top
        out.append(f'<text x="{label_w - 8}" y="{y + 19}" text-anchor="end" class="ink">{esc(name)}</text>'
                   f'<rect x="{label_w}" y="{y}" width="{max(wd, 2):.1f}" height="{bh}" rx="4" fill="var(--s1)" '
                   f'data-tip="{esc(name)}\n{fmt_int(n)} invites ({pct(n / top, 1)} of sent)"/>'
                   f'<text x="{label_w + wd + 8:.1f}" y="{y + 19}" class="ink">{fmt_int(n)}  '
                   f'<tspan>{pct(n / top, 1)} of sent{f" · {pct(pct_prev, 0)} of previous" if pct_prev else ""}</tspan></text>')
    out.append("</svg>")
    return "".join(out)


def week_hour_heatmap(grid, w=900):
    ml, mt, cw, ch = 40, 18, 33, 24
    mx = max(max(r) for r in grid)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    out = [f'<svg viewBox="0 0 {ml + cw * 24 + 4} {mt + ch * 7 + 4}" role="img">']
    for hr in range(0, 24, 3):
        out.append(f'<text x="{ml + hr * cw + cw / 2}" y="11" text-anchor="middle">{hr:02d}h</text>')
    for d in range(7):
        out.append(f'<text x="{ml - 8}" y="{mt + d * ch + 16}" text-anchor="end">{days[d]}</text>')
        for hr in range(24):
            v = grid[d][hr]
            out.append(f'<rect x="{ml + hr * cw + 1}" y="{mt + d * ch + 1}" width="{cw - 2}" height="{ch - 2}" rx="3" fill="var(--s1)" '
                       f'fill-opacity="{max(0.05, v / mx):.2f}" data-tip="{days[d]} {hr:02d}:00 · {v} purchases"/>')
    out.append("</svg>")
    return "".join(out)


def concentration(deciles, w=520, h=300):
    ml, mr, mt, mb = 44, 14, 10, 34
    X = lambda x: ml + (w - ml - mr) * x
    Y = lambda y: mt + (h - mt - mb) * (1 - y)
    pts = [(0, 0)] + [((i + 1) / 10, c) for i, c in enumerate(deciles)]
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" style="max-width:{w}px">']
    for k in range(0, 5):
        out.append(f'<line class="grid" x1="{ml}" x2="{w - mr}" y1="{Y(k / 4):.1f}" y2="{Y(k / 4):.1f}"/>'
                   f'<text x="{ml - 6}" y="{Y(k / 4) + 4:.1f}" text-anchor="end">{k * 25}%</text>')
        out.append(f'<text x="{X(k / 4):.1f}" y="{h - 16}" text-anchor="middle">{k * 25}%</text>')
    out.append(f'<text x="{ml + (w - ml - mr) / 2}" y="{h - 2}" text-anchor="middle">share of buyers (heaviest first)</text>')
    out.append(f'<line x1="{X(0)}" y1="{Y(0)}" x2="{X(1)}" y2="{Y(1)}" stroke="var(--muted)" stroke-dasharray="4 4"/>'
               f'<text x="{X(.62)}" y="{Y(.52)}" transform="rotate(-27 {X(.62)} {Y(.52)})">equal share</text>')
    out.append(f'<polyline fill="none" stroke="var(--s1)" stroke-width="2" points="{" ".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in pts)}"/>')
    for a, b in pts[1:]:
        out.append(f'<circle cx="{X(a):.1f}" cy="{Y(b):.1f}" r="4.5" fill="var(--s1)" stroke="var(--surface)" stroke-width="2" '
                   f'data-tip="Heaviest {int(a * 100)}% of buyers\n= {pct(b, 1)} of purchases"/>')
    out.append("</svg>")
    return "".join(out)


def grouped_bars(groups, series, w=420, h=260):
    """groups: [(label, [v per series])] values are 0..1 shares."""
    ml, mr, mt, mb = 40, 10, 24, 40
    gw = (w - ml - mr) / len(groups)
    bw = gw / (len(series) + 1)
    Y = lambda v: mt + (h - mt - mb) * (1 - v)
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" style="max-width:{w}px">']
    for k in range(5):
        out.append(f'<line class="grid" x1="{ml}" x2="{w - mr}" y1="{Y(k / 4):.1f}" y2="{Y(k / 4):.1f}"/>'
                   f'<text x="{ml - 6}" y="{Y(k / 4) + 4:.1f}" text-anchor="end">{k * 25}%</text>')
    for g, (lab, vals) in enumerate(groups):
        out.append(f'<text x="{ml + g * gw + gw / 2:.1f}" y="{h - 18}" text-anchor="middle">{esc(lab)}</text>')
        for s, ((nm, c), v) in enumerate(zip(series, vals)):
            x = ml + g * gw + bw * (s + .5)
            out.append(f'<path d="M{x:.1f},{Y(0)} V{Y(v) + 4:.1f} q0,-4 4,-4 h{bw - 10:.1f} q4,0 4,4 V{Y(0)} z" fill="var(--{c})" '
                       f'data-tip="{esc(nm)} · {esc(lab)}: {pct(v, 1)}"/>'
                       f'<text x="{x + bw / 2 - 1:.1f}" y="{Y(v) - 5:.1f}" text-anchor="middle" class="ink">{pct(v)}</text>')
    out.append("</svg>")
    return "".join(out)


def hbar_simple(rows, w=900, label_w=250, color="s2"):
    bh, gap = 18, 7
    h = len(rows) * (bh + gap)
    mx = max(v for _, v, _ in rows)
    sc = (w - label_w - 80) / mx
    out = [f'<svg viewBox="0 0 {w} {h}" role="img">']
    for i, (lab, v, tipx) in enumerate(rows):
        y = i * (bh + gap)
        out.append(f'<text x="{label_w - 8}" y="{y + 13}" text-anchor="end" class="ink">{esc(lab)}</text>'
                   f'<rect x="{label_w}" y="{y}" width="{max(v * sc, 2):.1f}" height="{bh}" rx="3" fill="var(--{color})" data-tip="{esc(tipx)}"/>'
                   f'<text x="{label_w + v * sc + 6:.1f}" y="{y + 13}">{fmt_int(v)}</text>')
    out.append("</svg>")
    return "".join(out)


# ----------------------------------------------------------------------------- report

def build(db: Path, dq_db: Path | None, out: Path) -> Path:
    con = duckdb.connect(str(db), read_only=True)
    from decimal import Decimal
    fl = lambda r: tuple(float(v) if isinstance(v, Decimal) else v for v in r)
    q = lambda s: [fl(r) for r in con.execute(s).fetchall()]
    one = lambda s: fl(con.execute(s).fetchone())

    users, buyers, purchases, revenue = one(
        "SELECT count(*), count(*) FILTER (WHERE n_purchases>0), sum(n_purchases), sum(revenue_usd) FROM gold.dim_user")
    d0, d1 = one("SELECT min(date_day), max(date_day) FROM gold.dim_date")
    raised, = one("SELECT sum(total_raised_usd) FROM gold.mart_school_fundraising")
    n_schools, = one("SELECT count(*) FROM gold.dim_school")
    f = q("SELECT stage, invites, pct_of_previous_stage FROM gold.mart_referral_funnel ORDER BY stage_order")
    conv = f[2][1] / f[0][1]

    kpis = [(fmt_int(users), "registered users"), (pct(buyers / users), "have purchased"), (fmt_int(purchases), "purchases"),
            (usd(revenue), "revenue"), (usd(raised), f"raised for {n_schools} schools"), (pct(conv, 1), "invite → signup")]

    # 1. activity
    act = q("SELECT activity_date, dau, wau, mau_28d, stickiness_dau_mau, is_warm_up FROM gold.mart_daily_platform_activity ORDER BY 1")
    dates = [r[0] for r in act]
    warm = sum(1 for r in act if r[5])
    post = [r for r in act if not r[5]]
    avg_dau = sum(r[1] for r in post) / len(post)
    avg_mau = sum(r[3] for r in post) / len(post)
    c_act = card(f"About {avg_dau:.0f} of {avg_mau:.0f} monthly actives show up on a given day ({pct(avg_dau / avg_mau)} stickiness)",
                 "Daily, weekly (7-day) and monthly (28-day) distinct users. 'Active' = bought something or sent a chatbot message.",
                 legend([("DAU", "s1"), ("WAU", "s2"), ("MAU (28d)", "s3")]) +
                 line_chart(dates, [("DAU", "s1", [r[1] for r in act]), ("WAU", "s2", [r[2] for r in act]),
                                    ("MAU", "s3", [r[3] for r in act])], shade_until=warm),
                 "gold.mart_daily_platform_activity")

    # 1b. chatbot
    c_chat = ""
    n_chat_msgs, = one("SELECT count(*) FROM silver.chat_messages")
    if n_chat_msgs:
        seg = {r[0]: r for r in q("SELECT segment, users, share_of_users, buyer_rate, avg_user_messages, avg_chat_sessions "
                                  "FROM gold.mart_chat_engagement")}
        ch, nc = seg.get("chatted"), seg.get("never_chatted")
        daily = q("SELECT buying_users, chatting_users FROM gold.mart_daily_platform_activity ORDER BY activity_date")
        both = (f"{pct(ch[3])} of chatbot users have bought something versus {pct(nc[3])} of users who never chatted. "
                if ch and nc else "")
        c_chat = card(f"{pct(ch[2]) if ch else '0%'} of users have used the chatbot, about {ch[4]:.0f} questions each" if ch else "Chatbot usage",
                      both + '<br><b>Caution:</b> the generator makes buyers more likely to chat, so this is a correlation the pipeline '
                      "recovers, not evidence that the bot drives sales. Message text stays in silver; gold holds counts only.",
                      legend([("Users who bought", "s1"), ("Users who chatted", "s2")]) +
                      line_chart(dates, [("Buying", "s1", [r[0] for r in daily]), ("Chatting", "s2", [r[1] for r in daily])]),
                      "gold.mart_daily_platform_activity · gold.mart_chat_engagement")

    # 2. retention
    raw = q("""SELECT cohort_period, cohort_size, period_number, retention_rate FROM gold.mart_cohort_retention_weekly
               WHERE cohort_type='new' AND is_complete_period ORDER BY 1,3""")
    by = {}
    for d, size, p, r in raw:
        by.setdefault((d, size), {})[p] = r
    rows = [(d, size, rates) for (d, size), rates in by.items()]
    wk = q("""SELECT period_number, sum(active_users)*1.0/sum(cohort_size) FROM gold.mart_cohort_retention_weekly
              WHERE cohort_type='new' AND is_complete_period GROUP BY 1 ORDER BY 1""")
    r1 = dict(wk)
    c_ret = card(f"{pct(r1[1])} of new users buy in their second week; {pct(r1[min(8, max(r1))])} still buy in week {min(8, max(r1))}",
                 "Each row is the cohort of users who signed up that week; each cell is the share who purchased N weeks later. "
                 "Incomplete (partial) weeks are left out. Denominator includes users who never bought, so week 0 is also activation.",
                 retention_heatmap(rows), "gold.mart_cohort_retention_weekly (cohort_type = new)")

    # 3. schools
    sch = q("""SELECT school_name || ' (' || state || ')', purchase_donations_usd, referral_bonus_usd, total_raised_usd
               FROM gold.mart_school_fundraising ORDER BY overall_rank LIMIT 10""")
    bonus_share = one("SELECT sum(referral_bonus_usd)/sum(total_raised_usd) FROM gold.mart_school_fundraising")[0]
    c_sch = card(f"Top school raised {usd(sch[0][3])}; referral bonuses are {pct(bonus_share)} of everything raised",
                 "Purchase donations (points × $0.01) plus $5 referral bonuses credited to the referrer's and the new user's school. "
                 "Both rates are assumptions, which is why the two parts are shown separately.",
                 legend([("Purchase donations", "s1"), ("Referral bonuses", "s2")]) +
                 hbar_stacked([(a, [b, c]) for a, b, c, _ in sch], [("Purchase donations", "s1"), ("Referral bonuses", "s2")]),
                 "gold.mart_school_fundraising (top 10 of %d)" % n_schools)

    # 4. funnel
    stages = [(n, i, p) for n, i, p in f]
    c_fun = card(f"{pct(f[2][1] / f[0][1], 1)} of invites become users, and {pct(f[3][1] / f[2][1])} of those buy within 7 days",
                 "Eligible invites only (invites to people who already had an account are excluded). Funnel stages are cumulative.",
                 funnel(stages), "gold.mart_referral_funnel")

    # 5. referral network + effectiveness
    top = q("""SELECT referrer_user_id, direct_conversions, network_size, max_depth, invites_sent
               FROM gold.mart_referral_network ORDER BY conversion_rank LIMIT 8""")
    n_ref, = one("SELECT count(*) FROM gold.mart_referral_network")
    share_top = one("""SELECT sum(direct_conversions) FILTER (WHERE conversion_rank <= 10) * 1.0 / sum(direct_conversions)
                       FROM gold.mart_referral_network""")[0]
    c_net = card(f"{n_ref} users invited anyone; the top 10 produced {pct(share_top)} of all signups",
                 "Direct signups per referrer. Hover for network size (all descendants) and depth.",
                 hbar_simple([(f"user {r[0]}  ·  {r[4]} invites", r[1], f"user {r[0]}\n{r[4]} invites → {r[1]} signups\nnetwork {r[2]} people, depth {r[3]}")
                              for r in top], label_w=180), "gold.mart_referral_network")
    eff = {r[0]: r for r in q("SELECT acquisition, users, activation_rate, repeat_rate, avg_days_to_first_purchase FROM gold.mart_referral_effectiveness")}
    if "referred" in eff and "organic" in eff:
        r_, o_ = eff["referred"], eff["organic"]
        c_eff = card(f"Referred users start buying in {r_[4]:.0f} days instead of {o_[4]:.0f}",
                     'Users who signed up at least 14 days before the end of data. <br><b>Caution:</b> the generator plants this effect '
                     "(referred users activate faster by construction), so this checks the pipeline recovers it; it is not a finding.",
                     legend([("Organic", "s2"), ("Referred", "s1")]) +
                     grouped_bars([("Activated (≥1 purchase)", [o_[2], r_[2]]), ("Repeat (≥2 purchases)", [o_[3], r_[3]])],
                                  [("Organic", "s2"), ("Referred", "s1")]),
                     "gold.mart_referral_effectiveness")
    else:
        c_eff = ""

    # 6. behaviour
    grid = [[0] * 24 for _ in range(7)]
    for d, h, n in q("SELECT day_of_week, hour_of_day, purchases FROM gold.mart_purchase_heatmap"):
        grid[d][h] = n
    best = max(((grid[d][h], d, h) for d in range(7) for h in range(24)))
    wk_share = sum(sum(grid[d]) for d in (5, 6)) / sum(sum(r) for r in grid)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    c_when = card(f"Weekends carry {pct(wk_share)} of purchases; the peak slot is {days[best[1]]} {best[2]:02d}:00",
                  "Purchases by weekday and hour.", week_hour_heatmap(grid), "gold.mart_purchase_heatmap")
    dec = q("SELECT cumulative_share FROM gold.mart_purchase_concentration ORDER BY decile")
    c_conc = card(f"The heaviest 10% of buyers make {pct(dec[0][0])} of all purchases",
                  "Cumulative share of purchases by decile of buyers (heaviest first). Equal shares would follow the dashed line.",
                  concentration([d[0] for d in dec]), "gold.mart_purchase_concentration")

    # 7. products
    prod = q("""SELECT product_name, purchases, repeat_buyer_rate, category FROM gold.mart_product_lifetime ORDER BY popularity_rank LIMIT 8""")
    c_prod = card(f"{prod[0][0]} is the top seller; {pct(prod[0][2])} of its buyers bought it again",
                  "Top products by purchases. Hover for repeat-buyer rate.",
                  hbar_simple([(f"{p[0]} · {p[3]}", p[1], f"{p[0]}\n{p[1]:,} purchases\n{pct(p[2], 1)} repeat buyers") for p in prod], label_w=280, color="s1"),
                  "gold.mart_product_lifetime")

    # 8. data quality
    run = one("SELECT run_id FROM meta.runs WHERE status='success' ORDER BY started_at DESC LIMIT 1")[0]
    n_checks, n_fail = one(f"SELECT count(*), count(*) FILTER (WHERE status='fail') FROM meta.dq_results WHERE run_id='{run}'")
    dq_body = f'<p class="note">Latest run <code>{esc(run)}</code>: <b>{n_checks}</b> checks evaluated, <b>{n_fail}</b> failed.</p>'
    dq_title = f"{n_checks} data-quality checks ran; {n_fail} failed on this data"
    if dq_db and Path(dq_db).exists():
        dcon = duckdb.connect(str(dq_db), read_only=True)
        drun = dcon.execute("SELECT run_id FROM meta.runs WHERE status='success' ORDER BY started_at DESC LIMIT 1").fetchone()[0]
        caught = dcon.execute(f"""SELECT entity||'.'||check_name, failed_rows, severity FROM meta.dq_results
                                  WHERE run_id='{drun}' AND failed_rows>0 ORDER BY failed_rows DESC LIMIT 12""").fetchall()
        quar = dcon.execute("SELECT (SELECT count(*) FROM quarantine.purchases)+(SELECT count(*) FROM quarantine.users)+(SELECT count(*) FROM quarantine.referrals)+(SELECT count(*) FROM quarantine.chat_messages)").fetchone()[0]
        dq_title = f"With ~1% injected bad data the checks quarantined {quar} rows and still produced a clean gold layer"
        dq_body = ('<p class="note">Same pipeline over a deliberately corrupted copy (duplicates, negative quantities, orphan keys, garbage '
                   "timestamps, self-referrals, bad e-mails…). Error rows are quarantined with reasons; warn rows load and are counted.</p>"
                   + legend([("error (quarantined)", "s2"), ("warn (loaded, counted)", "s1")]) +
                   hbar_simple([(c[0], c[1], f"{c[0]}\n{c[1]} rows · {c[2]}") for c in caught], label_w=300, color="s2").replace(
                       'fill="var(--s2)" data-tip="', 'fill="var(--s2)" data-tip="'))
        for c in caught:  # recolour warn rows
            if c[2] == "warn":
                dq_body = dq_body.replace(f'fill="var(--s2)" data-tip="{esc(c[0])}\n{c[1]} rows · warn"',
                                          f'fill="var(--s1)" data-tip="{esc(c[0])}\n{c[1]} rows · warn"')
    c_dq = card(dq_title, "", dq_body, "meta.dq_results · quarantine.*")

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Carton Caps analytics report</title><style>{CSS}</style></head><body><main>
<h1>Carton Caps analytics</h1>
<p class="sub">Generated from the gold layer of the pipeline · {d0:%b %d, %Y} – {d1:%b %d, %Y} · synthetic data.
Every headline below is computed from the tables, not typed in.</p>
<div class="kpis">{"".join(f'<div class="kpi"><b>{v}</b><span>{esc(l)}</span></div>' for v, l in kpis)}</div>
{c_act}{c_chat}{c_ret}{c_sch}{c_fun}{c_net}{c_eff}{c_when}{c_conc}{c_prod}{c_dq}
<p class="src">Built {date.today():%Y-%m-%d} by scripts/build_report.py · self-contained, no external requests.</p>
</main><div id="tip"></div><script>{JS}</script></body></html>"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "warehouse" / "carton_caps.duckdb"))
    ap.add_argument("--dq-db", default=str(ROOT / "warehouse" / "dirty_demo.duckdb"))
    ap.add_argument("--out", default=str(ROOT / "reports" / "index.html"))
    a = ap.parse_args()
    p = build(Path(a.db), Path(a.dq_db) if a.dq_db else None, Path(a.out))
    print(f"wrote {p} ({p.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()

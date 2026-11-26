"""Build the monthly management report (Markdown) and dashboard (HTML) for one month."""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from . import db, forecast, kpis, variance  # noqa: E402
from .commentary import Draft, eur, pct, write_commentary  # noqa: E402


@dataclass
class MonthReport:
    month: str
    kpi_table: pd.DataFrame
    pnl_ytd: pd.DataFrame
    cost_centres: pd.DataFrame
    flags: pd.DataFrame
    price_volume: pd.DataFrame
    commentary: Draft
    outlook: pd.DataFrame
    backtest: pd.DataFrame
    data_quality: list[str]
    monthly: pd.DataFrame
    forecast_revenue: pd.Series


def build(data_dir: Path, month: str, use_llm: bool = False) -> MonthReport:
    con = db.connect(data_dir)
    pva = db.query(con, "SELECT * FROM plan_vs_actual")
    if month not in set(pva["month"]):
        raise ValueError(f"no actuals for {month}; available: {pva['month'].min()} .. {pva['month'].max()}")
    a_drv = db.query(con, "SELECT * FROM actual_drivers")
    b_drv = db.query(con, "SELECT * FROM budget_drivers")
    accounts = db.query(con, "SELECT * FROM accounts")
    clean = db.query(con, "SELECT * FROM actuals_clean")
    budget = db.query(con, "SELECT * FROM budget")
    monthly = db.query(con, "SELECT * FROM pnl_month ORDER BY month")

    # KPIs for the month, actual vs plan.
    ka = kpis.month_kpis(pva, a_drv, month, "actual")
    kb = kpis.month_kpis(pva, b_drv, month, "budget")
    kpi_table = pd.DataFrame({"actual": ka, "budget": kb})
    kpi_table["variance"] = kpi_table["actual"] - kpi_table["budget"]

    # P&L year to date.
    ytd_months = sorted(m for m in pva["month"].unique() if m[:4] == month[:4] and m <= month)
    ytd = variance.by_cost_centre(pva, ytd_months).groupby("pnl_line")[["actual", "budget"]].sum()
    ytd.loc["Gross margin"] = ytd.loc["Revenue"] - ytd.loc["COGS"]
    ytd.loc["EBITDA"] = ytd.loc["Gross margin"] - ytd.loc["OpEx"]
    ytd = ytd.loc[["Revenue", "COGS", "Gross margin", "OpEx", "EBITDA"]]
    ytd["variance"] = ytd["actual"] - ytd["budget"]

    # Variances for the month.
    cc = variance.by_cost_centre(pva, [month])
    acc = variance.by_account(pva, [month])
    # Material variances are tested per account: a cost centre can look fine
    # overall while one account inside it is far off plan.
    flags = variance.flag(acc)
    pv = variance.price_volume(a_drv, b_drv, month)
    draft = write_commentary(month, flags, acc, pv, use_llm=use_llm)

    # Rolling forecast and full-year outlook.
    fc = forecast.rolling_forecast(clean, horizon=12)
    fc = fc.merge(accounts[["account", "pnl_line"]], on="account")
    year = month[:4]
    rest = fc[(fc["month"] > month) & (fc["month"].str.startswith(year))]
    fy_budget = budget.merge(accounts[["account", "pnl_line"]], on="account")
    fy_budget = fy_budget[fy_budget["month"].str.startswith(year)].groupby("pnl_line")["amount"].sum()
    outlook = pd.DataFrame(
        {
            "ytd_actual": ytd["actual"].reindex(["Revenue", "COGS", "OpEx"]),
            "forecast_rest": rest.groupby("pnl_line")["forecast"].sum().reindex(["Revenue", "COGS", "OpEx"]).fillna(0),
            "full_year_budget": fy_budget.reindex(["Revenue", "COGS", "OpEx"]),
        }
    )
    outlook["full_year_outlook"] = outlook["ytd_actual"] + outlook["forecast_rest"]
    ebitda = outlook.loc["Revenue"] - outlook.loc["COGS"] - outlook.loc["OpEx"]
    outlook.loc["EBITDA"] = ebitda
    outlook["vs_budget"] = outlook["full_year_outlook"] - outlook["full_year_budget"]

    revenue_accounts = accounts[accounts["pnl_line"] == "Revenue"]["account"].tolist()
    opex_accounts = accounts[accounts["pnl_line"] == "OpEx"]["account"].tolist()
    bt_rev = forecast.backtest(clean, budget, revenue_accounts).assign(target="Revenue")
    bt_opex = forecast.backtest(clean, budget, opex_accounts).assign(target="OpEx")
    bt = pd.concat([bt_rev, bt_opex], ignore_index=True)

    # Data quality.
    dq = []
    for _, r in db.query(con, "SELECT * FROM dq_unmapped").iterrows():
        dq.append(f"Account {r['account']} ({r['month']}, {eur(r['amount'])}) is not in the cost-centre mapping; excluded until mapped.")
    for _, r in db.query(con, "SELECT * FROM dq_duplicates").iterrows():
        dq.append(f"Account {r['account']} in {r['month']} was imported {r['copies']} times (EUR {r['amount']:,.2f}); counted once.")
    dq += kpis.check_tree(pva, a_drv, month, "actual")
    dq += kpis.check_tree(pva, b_drv, month, "budget")

    rev_fc = fc[fc["pnl_line"] == "Revenue"].groupby("month")["forecast"].sum()
    return MonthReport(month, kpi_table, ytd, cc, flags, pv, draft, outlook, bt, dq, monthly, rev_fc)


# ---------------------------------------------------------------- Markdown

def _fmt(v: float, kpi: str) -> str:
    if kpi.endswith("%"):
        return f"{v * 100:.1f}%"
    if kpi == "Customers":
        return f"{v:,.0f}"
    return f"EUR {v:,.0f}"


def to_markdown(r: MonthReport) -> str:
    L = [f"# Monthly management report: {r.month}", "", "*Lernwerk GmbH is a fictional company; all figures are synthetic.*", ""]
    L += ["## Summary", ""]
    L += [f"- {line}" for line in r.commentary.lines]
    src = "LLM draft, all numbers verified" if r.commentary.source == "llm" else "rule-based draft"
    L += ["", f"*Commentary: {src}.*"]
    if r.commentary.rejected_numbers:
        L += [f"*LLM draft rejected; unsupported numbers: {', '.join(r.commentary.rejected_numbers)}.*"]

    L += ["", "## KPIs (month)", "", "| KPI | Actual | Plan | Variance |", "|---|---:|---:|---:|"]
    for name, row in r.kpi_table.iterrows():
        var = f"{row['variance'] * 100:+.1f} pp" if name.endswith("%") else _fmt(row["variance"], name).replace("EUR ", "EUR +" if row["variance"] >= 0 else "EUR ")
        L.append(f"| {name} | {_fmt(row['actual'], name)} | {_fmt(row['budget'], name)} | {var} |")

    L += ["", "## Variance by cost centre (month)", "", "| P&L line | Cost centre | Actual | Plan | Variance | % |", "|---|---|---:|---:|---:|---:|"]
    for _, c in r.cost_centres.sort_values(["pnl_line", "cost_centre"]).iterrows():
        L.append(f"| {c['pnl_line']} | {c['cost_centre']} | {c['actual']:,.0f} | {c['budget']:,.0f} | {c['variance']:+,.0f} | {pct(c['variance_pct'])} |")

    L += ["", "## Material variances by account (at least 10% and EUR 5,000 off plan)", "", "| Account | Cost centre | Actual | Plan | Variance | % |", "|---|---|---:|---:|---:|---:|"]
    for _, f in r.flags.iterrows():
        L.append(f"| {f['account']} {f['name']} | {f['cost_centre']} | {f['actual']:,.0f} | {f['budget']:,.0f} | {f['variance']:+,.0f} | {pct(f['variance_pct'])} |")

    L += ["", "## Revenue: price and volume", "", "| Product | Customers (act / plan) | Avg price (act / plan) | Volume effect | Price effect | Total |", "|---|---:|---:|---:|---:|---:|"]
    for _, p in r.price_volume.iterrows():
        L.append(
            f"| {p['product']} | {p['customers_actual']:.0f} / {p['customers_budget']:.0f} | {p['price_actual']:,.0f} / {p['price_budget']:,.0f} | "
            f"{p['volume_effect']:+,.0f} | {p['price_effect']:+,.0f} | {p['revenue_variance']:+,.0f} |"
        )

    L += ["", f"## P&L year to date ({r.month[:4]})", "", "| Line | Actual | Plan | Variance |", "|---|---:|---:|---:|"]
    for line, row in r.pnl_ytd.iterrows():
        L.append(f"| {line} | {row['actual']:,.0f} | {row['budget']:,.0f} | {row['variance']:+,.0f} |")

    L += ["", f"## Full-year outlook {r.month[:4]} (actuals to date + rolling forecast)", "", "| Line | YTD actual | Forecast rest of year | Outlook | Budget | vs budget |", "|---|---:|---:|---:|---:|---:|"]
    for line, row in r.outlook.iterrows():
        L.append(f"| {line} | {row['ytd_actual']:,.0f} | {row['forecast_rest']:,.0f} | {row['full_year_outlook']:,.0f} | {row['full_year_budget']:,.0f} | {row['vs_budget']:+,.0f} |")

    L += ["", "## Forecast accuracy (backtest, last 6 months, one month ahead)", "", "| Target | Method | Mean abs. % error |", "|---|---|---:|"]
    for _, b in r.backtest.iterrows():
        L.append(f"| {b['target']} | {b['method']} | {b['mape'] * 100:.1f}% |")

    L += ["", "## Data quality", ""]
    L += [f"- {d}" for d in r.data_quality] or ["- No issues found."]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- HTML dashboard

def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def charts(r: MonthReport) -> list[tuple[str, str]]:
    m = r.monthly.set_index("month")
    out = []

    fig, ax = plt.subplots(figsize=(8, 3.2))
    ax.plot(m.index, m["revenue"] / 1000, color="#2f5bd3", marker="o", ms=3, label="Actual")
    ax.plot(m.index, m["revenue_budget"] / 1000, color="#9a9a9a", ls="--", label="Plan")
    ax.plot(r.forecast_revenue.index, r.forecast_revenue / 1000, color="#2f5bd3", ls=":", label="Rolling forecast")
    ax.set_ylabel("EUR k")
    ax.set_title("Revenue: actual, plan and rolling forecast", loc="left", fontsize=11)
    ax.tick_params(axis="x", rotation=90, labelsize=7)
    ax.legend(frameon=False, fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    out.append(("Revenue", _png(fig)))

    ebitda = (m["revenue"] - m["cogs"] - m["opex"]) / 1000
    fig, ax = plt.subplots(figsize=(8, 3.0))
    ax.bar(ebitda.index, ebitda, color=["#1f7a4d" if v >= 0 else "#b3261e" for v in ebitda])
    ax.axhline(0, color="#333", lw=0.6)
    ax.set_ylabel("EUR k")
    ax.set_title("EBITDA by month", loc="left", fontsize=11)
    ax.tick_params(axis="x", rotation=90, labelsize=7)
    ax.spines[["top", "right"]].set_visible(False)
    out.append(("EBITDA", _png(fig)))

    cc = r.cost_centres.copy()
    cc["label"] = cc["cost_centre"]
    cc["good"] = [variance.is_favourable(p, v) for p, v in zip(cc["pnl_line"], cc["variance"])]
    cc = cc.sort_values("variance")
    fig, ax = plt.subplots(figsize=(8, 2.8))
    ax.barh(cc["label"], cc["variance"] / 1000, color=["#1f7a4d" if g else "#b3261e" for g in cc["good"]])
    ax.axvline(0, color="#333", lw=0.6)
    ax.set_xlabel("EUR k vs plan (green = favourable)")
    ax.set_title(f"Variance by cost centre, {r.month}", loc="left", fontsize=11)
    ax.spines[["top", "right"]].set_visible(False)
    out.append(("Variance", _png(fig)))
    return out


def to_html(r: MonthReport) -> str:
    md = to_markdown(r)
    imgs = "".join(f'<img alt="{t}" src="data:image/png;base64,{b}">' for t, b in charts(r))
    rows = []
    for line in md.splitlines():
        if line.startswith("# "):
            rows.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("## "):
            rows.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("- "):
            rows.append(f"<li>{line[2:]}</li>")
        elif line.startswith("|") and not set(line) <= set("|-: "):
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        elif line.startswith("*") and line.endswith("*"):
            rows.append(f"<p class='note'>{line.strip('*')}</p>")
    body = "\n".join(rows)
    body = body.replace("</h2>\n<tr>", "</h2>\n<table><tr>").replace("</tr>\n<h2>", "</tr></table>\n<h2>")
    body = body.replace("</tr>\n<li>", "</tr></table>\n<li>")
    if body.rstrip().endswith("</tr>"):
        body += "</table>"
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Management report {r.month}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:980px;margin:32px auto;padding:0 20px;color:#1d1d1b}}
h1{{font-size:24px}} h2{{font-size:17px;margin-top:28px}} .note{{color:#6b6b66;font-size:13px}}
table{{border-collapse:collapse;width:100%;font-size:14px}} td{{border-bottom:1px solid #e2e2dc;padding:6px 8px}}
tr:first-child td{{font-weight:600;color:#6b6b66}} img{{width:100%;margin:8px 0 18px}} li{{margin:4px 0}}
</style></head><body>
<section>{imgs}</section>
{body}
</body></html>"""


def write(r: MonthReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"report-{r.month}.md"
    html_path = out_dir / f"dashboard-{r.month}.html"
    md_path.write_text(to_markdown(r))
    html_path.write_text(to_html(r))
    return md_path, html_path

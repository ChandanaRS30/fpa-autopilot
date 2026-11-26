"""KPI definitions and the KPI tree.

Every metric has exactly one definition here. Reports, dashboards and the
commentary all read from this module, so "revenue" or "CAC" can never mean two
different things in two different places.

KPI tree:

    EBITDA = Gross margin - OpEx
        Gross margin = Revenue - COGS
            Revenue = sum over products of (customers x average price)
            COGS    = tutor costs + payment fees
        OpEx = Marketing + Sales + Tech + G&A

    Contribution margin (per product) = product revenue - product tutor costs
                                        - payment fees allocated by revenue share
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Kpi:
    name: str
    definition: str
    unit: str


DEFINITIONS = [
    Kpi("Revenue", "Sum of revenue accounts (4xxx), net of VAT, by booking month.", "EUR"),
    Kpi("Customers", "New paying enrolments in the month, per product.", "count"),
    Kpi("Average price (AOV)", "Product revenue / product customers.", "EUR"),
    Kpi("Gross margin", "Revenue - COGS (tutor costs and payment fees).", "EUR"),
    Kpi("Gross margin %", "Gross margin / Revenue.", "%"),
    Kpi("Contribution margin", "Product revenue - product tutor costs - payment fees allocated by revenue share.", "EUR"),
    Kpi("EBITDA", "Gross margin - operating expenses (Marketing, Sales, Tech, G&A).", "EUR"),
    Kpi("CAC", "(Marketing + Sales cost) / new customers, same month.", "EUR"),
]


def month_kpis(pva: pd.DataFrame, drivers: pd.DataFrame, month: str, which: str) -> dict[str, float]:
    """KPIs for one month. `which` is 'actual' or 'budget'; `drivers` is the matching driver table."""
    m = pva[pva["month"] == month]
    d = drivers[drivers["month"] == month]
    by_line = m.groupby("pnl_line")[which].sum()
    revenue = float(by_line.get("Revenue", 0.0))
    cogs = float(by_line.get("COGS", 0.0))
    opex = float(by_line.get("OpEx", 0.0))
    gross = revenue - cogs
    customers = float(d["customers"].sum())
    acq_cost = float(m[m["cost_centre"].isin(["Marketing", "Sales"])][which].sum())

    out = {
        "Revenue": revenue,
        "Customers": customers,
        "Average price (AOV)": revenue / customers if customers else float("nan"),
        "Gross margin": gross,
        "Gross margin %": gross / revenue if revenue else float("nan"),
        "EBITDA": gross - opex,
        "CAC": acq_cost / customers if customers else float("nan"),
    }
    fees = float(m[m["account"] == "5100"][which].sum())
    for product in sorted(d["product"]):
        rev_p = float(m[(m["pnl_line"] == "Revenue") & (m["product"] == product)][which].sum())
        tutors_p = float(m[(m["pnl_line"] == "COGS") & (m["product"] == product)][which].sum())
        share = rev_p / revenue if revenue else 0.0
        out[f"Contribution margin {product}"] = rev_p - tutors_p - fees * share
    return out


def check_tree(pva: pd.DataFrame, drivers: pd.DataFrame, month: str, which: str = "actual") -> list[str]:
    """Reconciliation checks: the KPI tree must add up. Returns a list of problems (empty = OK)."""
    problems = []
    k = month_kpis(pva, drivers, month, which)
    m = pva[pva["month"] == month]
    d = drivers[drivers["month"] == month]

    driver_revenue = float((d["customers"] * d["avg_price"]).sum())
    if abs(driver_revenue - k["Revenue"]) > 0.01 * max(1.0, k["Revenue"]):
        problems.append(f"{month}: revenue from drivers {driver_revenue:,.0f} != booked revenue {k['Revenue']:,.0f}")

    contribution = sum(v for name, v in k.items() if name.startswith("Contribution margin"))
    if abs(contribution - k["Gross margin"]) > 0.01:
        problems.append(f"{month}: contribution margins {contribution:,.2f} != gross margin {k['Gross margin']:,.2f}")

    opex = float(m[m["pnl_line"] == "OpEx"][which].sum())
    if abs((k["Gross margin"] - opex) - k["EBITDA"]) > 0.01:
        problems.append(f"{month}: EBITDA does not equal gross margin - OpEx")
    return problems

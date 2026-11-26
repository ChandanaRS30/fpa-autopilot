"""Plan-vs-actual variance analysis.

* Variances by cost centre and account, for the month and year to date.
* Materiality flags: only variances that are both large in % and large in EUR.
* Revenue split into a volume effect (customers) and a price effect (average price).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Materiality:
    pct: float = 0.10  # at least 10% off plan ...
    eur: float = 5_000.0  # ... and at least EUR 5,000


def _with_pct(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["variance"] = df["actual"] - df["budget"]
    df["variance_pct"] = df["variance"] / df["budget"].where(df["budget"] != 0)
    return df


def by_cost_centre(pva: pd.DataFrame, months: list[str]) -> pd.DataFrame:
    m = pva[pva["month"].isin(months)]
    out = m.groupby(["pnl_line", "cost_centre"], as_index=False)[["actual", "budget"]].sum()
    return _with_pct(out)


def by_account(pva: pd.DataFrame, months: list[str]) -> pd.DataFrame:
    m = pva[pva["month"].isin(months)]
    out = m.groupby(["pnl_line", "cost_centre", "account", "name"], as_index=False)[["actual", "budget"]].sum()
    return _with_pct(out)


def flag(df: pd.DataFrame, materiality: Materiality = Materiality()) -> pd.DataFrame:
    """Rows whose variance is material, largest absolute EUR first."""
    mask = (df["variance_pct"].abs() >= materiality.pct) & (df["variance"].abs() >= materiality.eur)
    out = df[mask].copy()
    return out.reindex(out["variance"].abs().sort_values(ascending=False).index)


def is_favourable(pnl_line: str, variance: float) -> bool:
    """More revenue is good; more cost is bad."""
    return variance >= 0 if pnl_line == "Revenue" else variance <= 0


def price_volume(actual_drivers: pd.DataFrame, budget_drivers: pd.DataFrame, month: str) -> pd.DataFrame:
    """Split each product's revenue variance into volume and price effects.

    volume effect = (actual customers - budget customers) x budget price
    price effect  = (actual price - budget price) x actual customers
    The two effects add up exactly to the revenue variance.
    """
    a = actual_drivers[actual_drivers["month"] == month].set_index("product")
    b = budget_drivers[budget_drivers["month"] == month].set_index("product")
    rows = []
    for product in sorted(b.index):
        ca, pa = float(a.loc[product, "customers"]), float(a.loc[product, "avg_price"])
        cb, pb = float(b.loc[product, "customers"]), float(b.loc[product, "avg_price"])
        rows.append(
            {
                "product": product,
                "customers_actual": ca,
                "customers_budget": cb,
                "price_actual": pa,
                "price_budget": pb,
                "revenue_actual": ca * pa,
                "revenue_budget": cb * pb,
                "volume_effect": (ca - cb) * pb,
                "price_effect": (pa - pb) * ca,
            }
        )
    out = pd.DataFrame(rows)
    out["revenue_variance"] = out["revenue_actual"] - out["revenue_budget"]
    return out

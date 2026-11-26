"""Generate a synthetic P&L for a fictional online-education company.

Everything here is made up: "Lernwerk GmbH" does not exist. The data looks like a
real controlling export, with a chart of accounts, cost centres, two products, a
budget, 24 months of actuals and a few realistic data-quality problems:

* one booking on an account that is missing from the cost-centre mapping,
* one month where actuals were booked twice for an account (a duplicate import).

The 2026 plan assumed faster growth than the business delivers, so actuals
drift below plan through the year, which is what a rolling forecast is for.

October 2026 contains a planted story for the variance analysis to find:
a paid-ads campaign overspend, fewer coaching customers than planned, and a
course price increase.
"""
from __future__ import annotations

import csv
import random
from pathlib import Path

# Chart of accounts: account, name, P&L line, cost centre, product (if product-specific).
ACCOUNTS = [
    ("4000", "Revenue courses", "Revenue", "Revenue", "Courses"),
    ("4010", "Revenue coaching", "Revenue", "Revenue", "Coaching"),
    ("5000", "Tutor costs courses", "COGS", "Delivery", "Courses"),
    ("5010", "Tutor costs coaching", "COGS", "Delivery", "Coaching"),
    ("5100", "Payment and platform fees", "COGS", "Delivery", ""),
    ("6000", "Paid ads", "OpEx", "Marketing", ""),
    ("6010", "Content production", "OpEx", "Marketing", ""),
    ("6020", "Events and fairs", "OpEx", "Marketing", ""),
    ("6100", "Sales salaries", "OpEx", "Sales", ""),
    ("6110", "Sales commissions", "OpEx", "Sales", ""),
    ("6200", "Tech salaries", "OpEx", "Tech", ""),
    ("6210", "Cloud hosting", "OpEx", "Tech", ""),
    ("6220", "Software licences", "OpEx", "Tech", ""),
    ("6300", "Rent", "OpEx", "G&A", ""),
    ("6310", "Admin salaries", "OpEx", "G&A", ""),
    ("6320", "Legal and accounting", "OpEx", "G&A", ""),
]

PRODUCTS = {
    # base customers per month, price (EUR), tutor cost per customer (EUR)
    "Courses": (420, 1450.0, 610.0),
    "Coaching": (130, 890.0, 420.0),
}

# Enrolments peak in January and September, dip in summer and December.
SEASON = [1.25, 1.05, 1.00, 0.95, 0.90, 0.80, 0.75, 0.85, 1.20, 1.10, 1.00, 0.70]

FIRST_ACTUAL = (2024, 11)
N_ACTUAL = 24  # Nov 2024 .. Oct 2026
N_BUDGET = 26  # Nov 2024 .. Dec 2026 (budget runs to the end of the fiscal year)
FOCUS_MONTH = "2026-10"


def months(start: tuple[int, int], n: int) -> list[str]:
    y, m = start
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def plan_drivers(month: str, i: int) -> dict[str, tuple[float, float]]:
    """Budgeted customers and average price per product for month index i."""
    season = SEASON[int(month[5:]) - 1]
    growth = 1.018**i  # planned ~24% growth a year
    out = {}
    for product, (base, price, _) in PRODUCTS.items():
        price_step = 1.04 if month >= "2026-01" else 1.0  # planned 4% price increase in 2026
        out[product] = (round(base * season * growth), price * price_step)
    return out


def costs_from_drivers(drivers: dict[str, tuple[float, float]], i: int, rng: random.Random | None) -> dict[str, float]:
    """Cost per account given customers and prices. rng=None gives the plan (no noise)."""

    def noisy(x: float, sd: float = 0.04) -> float:
        return x * (1 + rng.gauss(0, sd)) if rng else x

    revenue = {p: c * price for p, (c, price) in drivers.items()}
    total_rev = sum(revenue.values())
    customers = sum(c for c, _ in drivers.values())
    headcount_growth = 1.012**i
    c = {
        "4000": revenue["Courses"],
        "4010": revenue["Coaching"],
        "5000": noisy(drivers["Courses"][0] * PRODUCTS["Courses"][2]),
        "5010": noisy(drivers["Coaching"][0] * PRODUCTS["Coaching"][2]),
        "5100": noisy(total_rev * 0.029, 0.02),
        "6000": noisy(customers * 95.0, 0.08),
        "6010": noisy(18_000.0, 0.10),
        "6020": noisy(6_000.0 if i % 3 else 22_000.0, 0.15),
        "6100": noisy(52_000.0 * headcount_growth, 0.01),
        "6110": noisy(total_rev * 0.03, 0.05),
        "6200": noisy(96_000.0 * headcount_growth, 0.01),
        "6210": noisy(9_500.0 * 1.01**i, 0.05),
        "6220": noisy(7_200.0, 0.03),
        "6300": 14_500.0,
        "6310": noisy(38_000.0 * 1.005**i, 0.01),
        "6320": noisy(8_000.0, 0.20),
    }
    return {k: round(v, 2) for k, v in c.items()}


def generate(out_dir: Path, seed: int = 42) -> None:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "accounts.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["account", "name", "pnl_line", "cost_centre", "product"])
        w.writerows(ACCOUNTS)

    budget_rows, budget_driver_rows = [], []
    for i, month in enumerate(months(FIRST_ACTUAL, N_BUDGET)):
        drivers = plan_drivers(month, i)
        for product, (cust, price) in drivers.items():
            budget_driver_rows.append([month, product, cust, round(price, 2)])
        for account, amount in costs_from_drivers(drivers, i, None).items():
            budget_rows.append([month, account, amount])

    actual_rows, actual_driver_rows = [], []
    for i, month in enumerate(months(FIRST_ACTUAL, N_ACTUAL)):
        plan = plan_drivers(month, i)
        drivers = {}
        # The plan assumed ~1.8% monthly growth; from 2026, real growth is ~1.0%,
        # so actuals drift below plan as the year goes on (as plans usually do).
        drift = (1.010 / 1.018) ** max(0, i - 13)
        for product, (cust, price) in plan.items():
            # Actual volume and price wander around the (drifting) plan.
            cust_a = cust * drift * (1 + rng.gauss(0.01, 0.05))
            price_a = price * (1 + rng.gauss(0.0, 0.015))
            if month == FOCUS_MONTH and product == "Coaching":
                cust_a = cust * 0.78  # coaching demand well below plan
            if month == FOCUS_MONTH and product == "Courses":
                price_a = price * 1.06  # price increase landed early
            drivers[product] = (max(1, round(cust_a)), round(price_a, 2))
            actual_driver_rows.append([month, product, drivers[product][0], drivers[product][1]])
        costs = costs_from_drivers(drivers, i, rng)
        # Ad spend follows the marketing budget, not actual demand.
        planned_ads = costs_from_drivers(plan, i, None)["6000"]
        costs["6000"] = round(planned_ads * (1 + rng.gauss(0, 0.05)), 2)
        if month == FOCUS_MONTH:
            costs["6000"] = round(planned_ads * 1.24, 2)  # autumn campaign overspend
        for account, amount in costs.items():
            actual_rows.append([month, account, amount])

    # Data-quality problems a real export would have.
    actual_rows.append(["2026-09", "6999", 4_250.00])  # account missing from the mapping
    dup = next(r for r in actual_rows if r[0] == "2026-08" and r[1] == "6210")
    actual_rows.append(list(dup))  # duplicate import of one booking

    for name, header, rows in [
        ("budget.csv", ["month", "account", "amount"], budget_rows),
        ("actuals.csv", ["month", "account", "amount"], actual_rows),
        ("budget_drivers.csv", ["month", "product", "customers", "avg_price"], budget_driver_rows),
        ("actual_drivers.csv", ["month", "product", "customers", "avg_price"], actual_driver_rows),
    ]:
        with open(out_dir / name, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)


if __name__ == "__main__":
    generate(Path("data"))
    print("wrote data/")

"""Rolling forecast per account, with a backtest against simple baselines.

Method ("seasonal trend"): next year's month = same month last year x recent
year-over-year growth, where growth is the ratio of the last 3 months to the
same 3 months a year earlier. It needs 15 months of history, handles the strong
January/September seasonality, and is easy to explain to a CFO.

The backtest replays the last months as if they were the future and compares
the method with two baselines: the budget, and "same as last month".
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _shift(month: str, k: int) -> str:
    y, m = int(month[:4]), int(month[5:])
    idx = y * 12 + (m - 1) + k
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def seasonal_trend(series: pd.Series, horizon: int) -> pd.Series:
    """Forecast `horizon` months after the last month of `series` (index = 'YYYY-MM')."""
    s = series.sort_index()
    last = s.index[-1]
    recent = sum(s.get(_shift(last, -k), np.nan) for k in range(3))
    year_ago = sum(s.get(_shift(last, -k - 12), np.nan) for k in range(3))
    growth = recent / year_ago if year_ago and not np.isnan(year_ago) else 1.0
    growth = float(np.clip(growth, 0.5, 2.0))  # guard against one-off spikes

    out = {}
    extended = s.copy()
    for h in range(1, horizon + 1):
        month = _shift(last, h)
        base = extended.get(_shift(month, -12), np.nan)
        out[month] = base * growth if not np.isnan(base) else float(s.iloc[-1])
        extended[month] = out[month]
    return pd.Series(out)


def rolling_forecast(actuals: pd.DataFrame, horizon: int = 12) -> pd.DataFrame:
    """actuals: month, account, amount (clean). Returns month, account, forecast."""
    rows = []
    for account, g in actuals.groupby("account"):
        series = g.set_index("month")["amount"].groupby(level=0).sum()
        for month, value in seasonal_trend(series, horizon).items():
            rows.append({"month": month, "account": account, "forecast": round(float(value), 2)})
    return pd.DataFrame(rows)


def _mape(actual: np.ndarray, predicted: np.ndarray) -> float:
    mask = actual != 0
    return float(np.mean(np.abs((actual[mask] - predicted[mask]) / actual[mask])))


def backtest(actuals: pd.DataFrame, budget: pd.DataFrame, accounts: list[str], test_months: int = 6) -> pd.DataFrame:
    """One-month-ahead backtest over the last `test_months` months, on total of `accounts`.

    Returns one row per method with the mean absolute percentage error (MAPE).
    """
    a = actuals[actuals["account"].isin(accounts)].groupby("month")["amount"].sum().sort_index()
    b = budget[budget["account"].isin(accounts)].groupby("month")["amount"].sum()
    months = list(a.index[-test_months:])

    truth, ours, naive, plan = [], [], [], []
    for month in months:
        history = a[a.index < month]
        truth.append(a[month])
        ours.append(float(seasonal_trend(history, 1).iloc[0]))
        naive.append(float(history.iloc[-1]))
        plan.append(float(b.get(month, np.nan)))
    t = np.array(truth)
    return pd.DataFrame(
        [
            {"method": "Seasonal trend (this tool)", "mape": _mape(t, np.array(ours))},
            {"method": "Budget", "mape": _mape(t, np.array(plan))},
            {"method": "Same as last month", "mape": _mape(t, np.array(naive))},
        ]
    )

"""Load the CSV exports into an in-memory SQLite database and create the reporting views."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

TABLES = {
    "accounts": {"account": str, "name": str, "pnl_line": str, "cost_centre": str, "product": str},
    "actuals": {"month": str, "account": str, "amount": float},
    "budget": {"month": str, "account": str, "amount": float},
    "actual_drivers": {"month": str, "product": str, "customers": int, "avg_price": float},
    "budget_drivers": {"month": str, "product": str, "customers": int, "avg_price": float},
}


def connect(data_dir: Path) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    for table, dtypes in TABLES.items():
        df = pd.read_csv(data_dir / f"{table}.csv", dtype=dtypes, keep_default_na=False)
        missing = set(dtypes) - set(df.columns)
        if missing:
            raise ValueError(f"{table}.csv is missing columns: {sorted(missing)}")
        df.to_sql(table, con, index=False)
    con.executescript((SQL_DIR / "views.sql").read_text())
    return con


def query(con: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=params)

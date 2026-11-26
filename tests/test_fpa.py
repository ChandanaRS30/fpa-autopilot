from pathlib import Path

import pandas as pd
import pytest

from fpa import commentary, db, forecast, generate, kpis, report, variance

MONTH = "2026-10"


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("data")
    generate.generate(d, seed=42)
    return d


@pytest.fixture(scope="session")
def con(data_dir):
    return db.connect(data_dir)


@pytest.fixture(scope="session")
def pva(con):
    return db.query(con, "SELECT * FROM plan_vs_actual")


# ---------------------------------------------------------------- SQL layer and data quality

def test_sql_totals_match_raw_data(con, data_dir):
    raw = pd.read_csv(data_dir / "actuals.csv", dtype={"account": str})
    raw = raw.drop_duplicates()  # the planted duplicate counts once
    mapped = raw[raw["account"].isin(pd.read_csv(data_dir / "accounts.csv", dtype=str)["account"])]
    sql_total = db.query(con, "SELECT SUM(actual) AS t FROM plan_vs_actual")["t"][0]
    assert sql_total == pytest.approx(mapped["amount"].sum())


def test_detects_unmapped_account_and_duplicate(con):
    unmapped = db.query(con, "SELECT * FROM dq_unmapped")
    dups = db.query(con, "SELECT * FROM dq_duplicates")
    assert list(unmapped["account"]) == ["6999"]
    assert list(zip(dups["month"], dups["account"])) == [("2026-08", "6210")]


def test_missing_column_is_a_clear_error(tmp_path, data_dir):
    for f in data_dir.iterdir():
        (tmp_path / f.name).write_text(f.read_text())
    (tmp_path / "budget.csv").write_text("month,acct,amount\n2026-01,4000,1\n")
    with pytest.raises(ValueError, match="budget.csv is missing columns"):
        db.connect(tmp_path)


# ---------------------------------------------------------------- KPI tree

@pytest.mark.parametrize("which,drivers_table", [("actual", "actual_drivers"), ("budget", "budget_drivers")])
def test_kpi_tree_reconciles_every_month(con, pva, which, drivers_table):
    drivers = db.query(con, f"SELECT * FROM {drivers_table}")
    for month in pva["month"].unique():
        assert kpis.check_tree(pva, drivers, month, which) == []


def test_every_kpi_has_one_definition():
    names = [k.name for k in kpis.DEFINITIONS]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------- Variance

def test_price_and_volume_effects_add_up_to_revenue_variance(con):
    pv = variance.price_volume(db.query(con, "SELECT * FROM actual_drivers"), db.query(con, "SELECT * FROM budget_drivers"), MONTH)
    assert (pv["volume_effect"] + pv["price_effect"]).to_numpy() == pytest.approx(pv["revenue_variance"].to_numpy())


def test_materiality_needs_both_percent_and_euro():
    df = pd.DataFrame(
        {
            "pnl_line": ["OpEx"] * 3,
            "cost_centre": ["A", "B", "C"],
            "actual": [120.0, 1_100_000.0, 30_000.0],
            "budget": [100.0, 1_000_000.0, 20_000.0],
        }
    )
    df["variance"] = df["actual"] - df["budget"]
    df["variance_pct"] = df["variance"] / df["budget"]
    # A: +20% but only EUR 20. B: EUR 100k but +10% exactly -> flagged. C: +50% and EUR 10k -> flagged, biggest EUR first.
    assert list(variance.flag(df)["cost_centre"]) == ["B", "C"]


def test_finds_the_planted_october_story(pva):
    flags = variance.flag(variance.by_account(pva, [MONTH]))
    assert {"6000", "4010"} <= set(flags["account"])
    ads = flags[flags["account"] == "6000"].iloc[0]
    assert ads["variance"] > 0 and not variance.is_favourable("OpEx", ads["variance"])


# ---------------------------------------------------------------- Forecast

def test_seasonal_trend_on_a_known_series():
    months = [f"2025-{m:02d}" for m in range(1, 13)] + [f"2026-{m:02d}" for m in range(1, 4)]
    values = [100.0] * 12 + [110.0] * 3  # last 3 months are 10% above a year earlier
    s = pd.Series(values, index=months)
    fc = forecast.seasonal_trend(s, 2)
    assert list(fc.index) == ["2026-04", "2026-05"]
    assert fc.to_numpy() == pytest.approx([110.0, 110.0])


def test_backtest_reports_all_methods(con):
    clean = db.query(con, "SELECT * FROM actuals_clean")
    budget = db.query(con, "SELECT * FROM budget")
    bt = forecast.backtest(clean, budget, ["4000", "4010"])
    assert set(bt["method"]) == {"Seasonal trend (this tool)", "Budget", "Same as last month"}
    assert (bt["mape"] >= 0).all()


# ---------------------------------------------------------------- Commentary fact check

def _inputs(pva, con):
    acc = variance.by_account(pva, [MONTH])
    flags = variance.flag(acc)
    pv = variance.price_volume(db.query(con, "SELECT * FROM actual_drivers"), db.query(con, "SELECT * FROM budget_drivers"), MONTH)
    return flags, acc, pv


def test_rule_based_numbers_all_pass_the_fact_check(pva, con):
    flags, acc, pv = _inputs(pva, con)
    text = "\n".join(commentary.rule_based(flags, acc, pv))
    assert commentary.unsupported_numbers(text, commentary.allowed_numbers(flags, acc, pv), MONTH) == []


def test_llm_text_with_correct_numbers_is_used(pva, con, monkeypatch):
    flags, acc, pv = _inputs(pva, con)
    ads = flags[flags["account"] == "6000"].iloc[0]
    good = f"Paid ads ran {commentary.eur(ads['variance'])} ({commentary.pct(ads['variance_pct'])}) over plan in October 2026 because of the autumn campaign."
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    draft = commentary.write_commentary(MONTH, flags, acc, pv, use_llm=True, llm=lambda *a: good)
    assert draft.source == "llm"
    assert draft.lines == [good]


def test_llm_text_with_an_invented_number_is_rejected(pva, con, monkeypatch):
    flags, acc, pv = _inputs(pva, con)
    bad = "Paid ads ran +EUR 31.7k (+41.0%) over plan because of the autumn campaign."
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    draft = commentary.write_commentary(MONTH, flags, acc, pv, use_llm=True, llm=lambda *a: bad)
    assert draft.source == "rules"
    assert "31.7k" in " ".join(draft.rejected_numbers)


def test_without_api_key_falls_back_to_rules(pva, con, monkeypatch):
    flags, acc, pv = _inputs(pva, con)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    called = []
    draft = commentary.write_commentary(MONTH, flags, acc, pv, use_llm=True, llm=lambda *a: called.append(1) or "")
    assert draft.source == "rules" and not called


# ---------------------------------------------------------------- End to end

def test_report_end_to_end(data_dir, tmp_path):
    rep = report.build(data_dir, MONTH)
    md, html = report.write(rep, tmp_path)
    text = md.read_text()
    assert "Paid ads (Marketing) +EUR" in text
    assert "not in the cost-centre mapping" in text
    assert html.read_text().count("data:image/png;base64") == 3
    # Full-year outlook: EBITDA row is revenue - COGS - OpEx.
    o = rep.outlook
    assert o.loc["EBITDA", "full_year_outlook"] == pytest.approx(
        o.loc["Revenue", "full_year_outlook"] - o.loc["COGS", "full_year_outlook"] - o.loc["OpEx", "full_year_outlook"]
    )


def test_unknown_month_is_a_clear_error(data_dir):
    with pytest.raises(ValueError, match="no actuals for 2030-01"):
        report.build(data_dir, "2030-01")

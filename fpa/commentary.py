"""Variance commentary: a rule-based draft, an optional LLM draft, and a fact check.

The rule-based draft always works and is fully traceable. The LLM draft
(any OpenAI-compatible API, enabled with OPENAI_API_KEY) reads better, but an
LLM can invent or misquote numbers. So every number in the LLM text is checked
against the computed variances; if any number has no match, the LLM draft is
rejected and the rule-based draft is used instead.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass

import pandas as pd

from .variance import is_favourable


def eur(x: float) -> str:
    """+EUR 12.4k / -EUR 850, the format used in all comments."""
    sign = "+" if x >= 0 else "-"
    a = abs(x)
    return f"{sign}EUR {a / 1000:.1f}k" if a >= 1000 else f"{sign}EUR {a:.0f}"


def pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def rule_based(account_flags: pd.DataFrame, accounts: pd.DataFrame, pv: pd.DataFrame) -> list[str]:
    """Revenue first (split into price and volume), then one sentence per material cost account."""
    lines = []
    rev = accounts[accounts["pnl_line"] == "Revenue"]
    total, budget = rev["actual"].sum(), rev["budget"].sum()
    parts = [
        f"{p['product']} {eur(p['revenue_variance'])}: volume {eur(p['volume_effect'])} "
        f"({p['customers_actual']:.0f} vs {p['customers_budget']:.0f} customers), "
        f"price {eur(p['price_effect'])} (EUR {p['price_actual']:,.0f} vs {p['price_budget']:,.0f})"
        for _, p in pv.iterrows()
    ]
    lines.append(f"Revenue {eur(total - budget)} ({pct((total - budget) / budget)}) vs plan. " + "; ".join(parts) + ".")

    for _, r in account_flags[account_flags["pnl_line"] != "Revenue"].iterrows():
        tone = "favourable" if is_favourable(r["pnl_line"], r["variance"]) else "unfavourable"
        lines.append(
            f"{r['name']} ({r['cost_centre']}) {eur(r['variance'])} ({pct(r['variance_pct'])}) vs plan, {tone}."
        )
    return lines


# ---------------------------------------------------------------- LLM draft + fact check

NUMBER = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?\s*(?:%|k\b)?")


def allowed_numbers(flags: pd.DataFrame, accounts: pd.DataFrame, pv: pd.DataFrame) -> set[str]:
    """Every number the commentary may legitimately quote, in the formats it would use."""
    vals: set[str] = set()

    def add_eur(x: float) -> None:
        vals.add(f"{abs(x) / 1000:.1f}k")
        vals.add(f"{abs(x):,.0f}")
        vals.add(f"{abs(x):.0f}")

    def add_pct(x: float) -> None:
        vals.add(f"{abs(x) * 100:.1f}%")
        vals.add(f"{abs(x) * 100:.0f}%")

    for df in (flags, accounts):
        for _, r in df.iterrows():
            for col in ("actual", "budget", "variance"):
                add_eur(r[col])
            if pd.notna(r["variance_pct"]):
                add_pct(r["variance_pct"])
    rev = accounts[accounts["pnl_line"] == "Revenue"]
    add_eur(rev["actual"].sum() - rev["budget"].sum())
    add_pct((rev["actual"].sum() - rev["budget"].sum()) / rev["budget"].sum())
    for _, p in pv.iterrows():
        for col in ("volume_effect", "price_effect", "revenue_variance", "price_actual", "price_budget"):
            add_eur(p[col])
        for col in ("customers_actual", "customers_budget"):
            vals.add(f"{p[col]:.0f}")
    return vals


def unsupported_numbers(text: str, allowed: set[str], month: str) -> list[str]:
    """Numbers in `text` that do not match any computed value. Dates are ignored."""
    year, mon = month[:4], str(int(month[5:]))
    bad = []
    for raw in NUMBER.findall(text):
        token = raw.replace(" ", "").lstrip("+-")
        if token in {year, mon, month[5:]} or token in allowed:
            continue
        bad.append(raw.strip())
    return bad


@dataclass
class Draft:
    lines: list[str]
    source: str  # "llm" or "rules"
    rejected_numbers: list[str]


def llm_draft(payload: dict, model: str, api_key: str, base_url: str) -> str:
    prompt = (
        "You are an FP&A analyst. Write one short sentence per flagged variance for the CFO's monthly report. "
        "Name the driver. Use ONLY numbers that appear in the JSON, formatted like +EUR 12.4k and +18.2%. "
        "No introduction, one sentence per line.\n\n" + json.dumps(payload, indent=1)
    )
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps({"model": model, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["choices"][0]["message"]["content"]


def write_commentary(
    month: str,
    flags: pd.DataFrame,  # material account-level variances
    accounts: pd.DataFrame,
    pv: pd.DataFrame,
    use_llm: bool = False,
    llm=llm_draft,
) -> Draft:
    rules = rule_based(flags, accounts, pv)
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not use_llm or not api_key:
        return Draft(rules, "rules", [])

    payload = {
        "month": month,
        "material_variances": flags[["pnl_line", "cost_centre", "name", "actual", "budget", "variance", "variance_pct"]].round(3).to_dict("records"),
        "accounts": accounts[["cost_centre", "name", "actual", "budget", "variance", "variance_pct"]].round(3).to_dict("records"),
        "revenue_price_volume": pv.round(2).to_dict("records"),
        "draft": rules,
    }
    text = llm(payload, os.environ.get("OPENAI_MODEL", "gpt-4o-mini"), api_key, os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    bad = unsupported_numbers(text, allowed_numbers(flags, accounts, pv), month)
    if bad:
        return Draft(rules, "rules", bad)
    return Draft([line.strip("-• ").strip() for line in text.splitlines() if line.strip()], "llm", [])

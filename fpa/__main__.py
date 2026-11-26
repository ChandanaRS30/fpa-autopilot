"""Command line: python -m fpa generate | report --month YYYY-MM | kpis"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import generate, kpis, report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m fpa", description="Automated monthly FP&A reporting.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write the synthetic P&L data set")
    g.add_argument("--out", type=Path, default=Path("data"))
    g.add_argument("--seed", type=int, default=42)

    r = sub.add_parser("report", help="build the monthly report and dashboard")
    r.add_argument("--month", required=True, help="YYYY-MM")
    r.add_argument("--data", type=Path, default=Path("data"))
    r.add_argument("--out", type=Path, default=Path("reports"))
    r.add_argument("--llm", action="store_true", help="draft commentary with an LLM (needs OPENAI_API_KEY); numbers are fact-checked")

    sub.add_parser("kpis", help="print the KPI definitions")

    args = ap.parse_args(argv)
    if args.cmd == "generate":
        generate.generate(args.out, args.seed)
        print(f"wrote synthetic data to {args.out}/")
    elif args.cmd == "report":
        try:
            rep = report.build(args.data, args.month, use_llm=args.llm)
        except (ValueError, FileNotFoundError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        md, html = report.write(rep, args.out)
        print(f"wrote {md} and {html}")
    elif args.cmd == "kpis":
        for k in kpis.DEFINITIONS:
            print(f"{k.name} [{k.unit}]: {k.definition}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

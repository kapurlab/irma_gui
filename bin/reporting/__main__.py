"""CLI: regenerate the report (and stats workbook) for an existing IRMA run dir.

    python -m reporting --outdir <dir> --sample <name>
    python -m reporting --outdir <dir> --sample <name> --reports-only

Everything the report needs is already in the run directory, so a finished run
can be re-rendered with a newer report layout without re-running IRMA.
--reports-only keeps the existing stats workbook rather than writing a second,
newly timestamped copy of it beside the original.
"""
import argparse
import json
from pathlib import Path

from . import build

ap = argparse.ArgumentParser(description="Build IRMA stats.xlsx + report.pdf for a run dir.")
ap.add_argument("--outdir", type=Path, required=True)
ap.add_argument("--sample", required=True)
ap.add_argument("--reports-only", action="store_true",
                help="re-render report.html/report.pdf only; keep the existing stats workbook")
args = ap.parse_args()
print(json.dumps(build(args.outdir, args.sample, write_xlsx=not args.reports_only), indent=2))

#!/usr/bin/env python3
"""Collect and reconcile project identities and five official milestones."""
import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from land_tracker_enrichment import enrich
from update_land_tracker import ROOT, DASHBOARD, ValidationError, atomic_write, embed_rows, read_rows, validate_rows


def run(args):
    page_path = args.page or args.root / DASHBOARD
    page = page_path.read_text(encoding="utf-8")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    rows = read_rows(page)[0]
    validate_rows(rows, today)
    proposed, evidence, report = enrich(rows, args.root, args.report_dir / "enrichment", today,
                                       args.snapshot_dir, args.resume_dir, getattr(args, 'checkpoint_dir', None))
    validate_rows(proposed, today)
    output = embed_rows(page, proposed)
    if page_path.read_text(encoding="utf-8") != page:
        raise ValidationError("Page changed during enrichment; retry")
    atomic_write(args.report_dir / "proposed.html", output)
    if args.apply:
        atomic_write(args.root / "data/land_tracker_enrichment.json", json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        atomic_write(args.root / DASHBOARD, output)
    print(json.dumps({"status": "passed", "apply": args.apply, "rows": len(rows), "changes": len(report["changes"]), "review": len(report["review"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--page", type=Path, help="Optional transaction-stage proposed HTML")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "reports/land-tracker")
    parser.add_argument("--snapshot-dir", type=Path, help="Replay enrichment/raw without network")
    parser.add_argument("--resume-dir", type=Path, help="Reuse explicitly selected snapshots, fetching missing requests")
    parser.add_argument("--checkpoint-dir", type=Path, help="Reuse same-day validated queries (maximum age six hours)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        raise SystemExit(run(args))
    except (ValueError, OSError, KeyError) as error:
        print("::error title=Land enrichment::" + str(error).replace("%", "%25").replace("\n", "%0A"))
        raise SystemExit(1)

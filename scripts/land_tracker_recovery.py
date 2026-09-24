"""Bounded transport retries and same-day scheduled recovery decisions."""
import argparse
import copy
import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from update_land_tracker import ROOT, DASHBOARD, run as run_transaction

TZ = ZoneInfo('Asia/Shanghai')


def should_collect(event, feed, now):
    if event != 'schedule':
        return True
    try:
        checked = datetime.fromisoformat(feed['checkedAtText'])
        return not (feed['status'] == 'ready' and feed['schemaVersion'] == 1
                    and isinstance(feed['rows'], list)
                    and 0 < feed['recordCount'] == len(feed['rows']) <= 1000
                    and checked.tzinfo is not None and checked <= now
                    and checked.astimezone(TZ).date() == now.astimezone(TZ).date()
                    and feed['expiresAt'] > now.timestamp() * 1000)
    except (KeyError, ValueError, TypeError):
        return True


def collect(args):
    directory = args.report_dir
    directory.mkdir(parents=True, exist_ok=True)
    for filename in ('proposed.html', 'proposed_sources.json'):
        (directory / filename).unlink(missing_ok=True)
    for attempt in range(3):
        current = copy.copy(args)
        current.snapshot_dir = getattr(args, 'snapshot_dir', None)
        current.report_dir = directory / 'transaction-attempts' / str(attempt + 1)
        status = run_transaction(current)
        report = json.loads((current.report_dir / 'report.json').read_text())
        for filename in ('report.json', 'report.md'):
            source = current.report_dir / filename
            if source.exists():
                shutil.copyfile(source, directory / filename)
        if status == 0:
            for filename in ('proposed.html', 'proposed_sources.json'):
                source = current.report_dir / filename
                if source.exists():
                    shutil.copyfile(source, directory / filename)
            return 0
        if not report.get('retryable') or attempt == 2:
            return 1
        delay = (60, 180)[attempt]
        print(f'Temporary source failure; retry {attempt + 2}/3 after {delay}s', flush=True)
        time.sleep(delay)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'collect'))
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--report-dir', type=Path, default=ROOT / 'reports/land-tracker')
    parser.add_argument('--event', default=os.environ.get('GITHUB_EVENT_NAME', 'manual'))
    parser.add_argument('--github-output', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.command == 'collect':
        return collect(args)
    current = datetime.now(TZ)
    try:
        feed = json.loads((args.root / DASHBOARD.parent / 'feishu-feed.json').read_text())
    except (OSError, ValueError):
        feed = {}
    required = should_collect(args.event, feed, current)
    output = f'should_run={str(required).lower()}\ndate={current.date().isoformat()}\n'
    print(output, end='')
    if args.github_output:
        with args.github_output.open('a') as target:
            target.write(output)
    if not required:
        message = 'Same-day validated feed already exists; scheduled recovery skipped.\n'
        print(message)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as target:
                target.write(message)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

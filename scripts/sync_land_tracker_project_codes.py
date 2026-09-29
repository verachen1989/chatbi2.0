"""Backfill verified project codes from a fresh 6404 snapshot, never publish."""
import argparse
import copy
import fcntl
import json
import re
import subprocess
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from import_land_tracker_project_links import (
    DASHBOARD, PROJECT_FIELDS, ROOT, ValidationError, atomic_write, embed_rows,
    fetch_records, fingerprint, lark, merge_project_links, read_rows,
)
from sync_land_tracker_project_links import BASE_URL, TZ, assert_unchanged


def name_key(value):
    return re.sub(r'[\s\u00b7\u2022]', '', unicodedata.normalize('NFKC', value)).upper()


def validate_source(source):
    try:
        if not isinstance(source, dict):
            raise ValueError('Invalid source object')
        checked = datetime.fromisoformat(source['retrievedAt'])
        valid = (source['datasetCode'] == '6404' and source['datasourceCode'] == 'sr_ads_tdda'
                 and source.get('complete') is True and checked.tzinfo is not None
                 and -timedelta(minutes=5) <= datetime.now(TZ) - checked <= timedelta(hours=48)
                 and isinstance(source['records'], list) and bool(source['records']))
        if not valid:
            raise ValueError('Invalid source metadata')
        for row in source['records']:
            if (not isinstance(row, dict)
                    or not isinstance(row.get('projectName'), str) or not name_key(row['projectName'])
                    or not isinstance(row.get('projectCode'), str)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', row['projectCode'])):
                raise ValueError('Invalid name or code')
    except (KeyError, TypeError, ValueError) as error:
        raise ValidationError('6404 snapshot is stale, incomplete, invalid or from another source') from error


def plan_codes(projects, source, previous):
    validate_source(source)
    index = defaultdict(set)
    for row in source['records']:
        index[name_key(row['projectName'])].add(row['projectCode'])
    owners = defaultdict(set)
    state = copy.deepcopy(previous)
    items, updates = [], {}
    for project in projects:
        record_id = project['record_id']
        code = project.get('projectCode') or ''
        if not isinstance(code, str):
            raise ValidationError('projectCode field type changed')
        if code.strip():
            owners[code.strip()].add(record_id)
    for project in projects:
        record_id = project['record_id']
        names = [project.get(field) for field in PROJECT_FIELDS[:3] if project.get(field)]
        if any(not isinstance(n, str) for n in names):
            raise ValidationError('Project name field type changed')
        candidates = sorted(set().union(*(index[name_key(n)] for n in names)))
        item = {'recordId': record_id, 'projectName': names[0] if names else '', 'candidates': candidates}
        items.append(item)
        old = state.setdefault(record_id, {})
        code = (project.get('projectCode') or '').strip()
        if code:
            old['lastSeenCode'] = code
            item['decision'] = 'preserved_existing'
        elif old.get('lastWrittenCode') or old.get('lastSeenCode') or old.get('manualClear'):
            old['manualClear'] = True
            item['decision'] = 'preserved_manual_clear'
        elif project.get('地块关联状态') != ['已确认'] or not project.get('关联地块'):
            item['decision'] = 'unconfirmed'
        elif len(candidates) != 1:
            item['decision'] = 'ambiguous' if candidates else 'missing'
        else:
            item['decision'] = 'unique'
            owners[candidates[0]].add(record_id)
    for item in items:
        if item['decision'] != 'unique':
            continue
        code, record_id = item['candidates'][0], item['recordId']
        if len(owners[code]) > 1:
            item['decision'] = 'code_used_by_multiple_records'
            continue
        updates[record_id] = {'projectCode': code}
        state[record_id].update(lastWrittenCode=code, lastSeenCode=code)
    return {'updates': updates, 'items': items, 'state': state}


def verify_result(before, current, updates):
    expected = [dict(record, **updates.get(record['record_id'], {})) for record in before]
    assert_unchanged(expected, current)


def load_state(path, context, initialize):
    if not path.exists():
        if not initialize:
            raise ValidationError('Code-sync state is missing; restore it, do not silently recreate it')
        return {'version': 1, 'context': context, 'projects': {}}
    state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('version') != 1 or state.get('context') != context or not isinstance(state.get('projects'), dict):
        raise ValidationError('Code-sync state belongs to another table or has changed')
    return state


def run(args):
    now = datetime.now(TZ)
    journal = args.report_dir / now.strftime('%Y%m%d-%H%M%S-%f')
    journal.mkdir(parents=True, exist_ok=False)
    source = json.loads(args.source.read_text(encoding='utf-8'))
    validate_source(source)
    original = args.page.read_text(encoding='utf-8')
    resolved = lark(args.lark_cli, '+url-resolve', '--url', args.base_url)
    if resolved.get('block_type') != 'table':
        raise ValidationError('Project URL must select a table')
    base, table = resolved['base_token'], resolved['table_id']
    fields = lark(args.lark_cli, '+field-list', '--base-token', base, '--table-id', table)['fields']
    schema = {field['name']: field for field in fields}
    if (any(schema.get(n, {}).get('type') != 'text' for n in PROJECT_FIELDS[:4])
            or schema.get('关联地块', {}).get('type') != 'link'
            or not schema['关联地块'].get('link_table')
            or schema.get('地块关联状态', {}).get('type') != 'select'
            or schema['地块关联状态'].get('multiple') is not False):
        raise ValidationError('Project code/name/relation schema changed')
    context = {'base': base, 'table': table, 'datasetCode': '6404'}
    state = load_state(args.state, context, args.init_state)
    names = list(schema)
    projects = fetch_records(args.lark_cli, base, table, names)
    lands = fetch_records(args.lark_cli, base, schema['关联地块']['link_table'], ['地块编号'])
    atomic_write(journal / 'before.json', json.dumps(projects, ensure_ascii=False, indent=2))
    # An interrupted response must be read back, never replayed as another write.
    if state.get('pending'):
        pending = state['pending']
        verify_result(pending['before'], projects, pending['updates'])
        state['projects'] = pending['nextProjects']
        state.pop('pending')
        if args.apply:
            atomic_write(args.state, json.dumps(state, ensure_ascii=False, indent=2))
    plan = plan_codes(projects, source, state['projects'])
    projected = [dict(record, **plan['updates'].get(record['record_id'], {})) for record in projects]
    merged = merge_project_links(read_rows(original)[0], lands, projected)
    atomic_write(journal / 'plan.json', json.dumps(plan, ensure_ascii=False, indent=2))
    atomic_write(journal / 'source.json', json.dumps(source, ensure_ascii=False, indent=2))
    atomic_write(journal / 'proposed.html', embed_rows(original, merged))
    if args.apply:
        assert_unchanged(projects, fetch_records(args.lark_cli, base, table, names))
        assert_unchanged(lands, fetch_records(args.lark_cli, base, schema['关联地块']['link_table'], ['地块编号']))
        if args.page.read_text(encoding='utf-8') != original:
            raise ValidationError('Local page changed before write')
        if plan['updates']:
            state['pending'] = {'before': projects, 'updates': plan['updates'], 'nextProjects': plan['state']}
            atomic_write(args.state, json.dumps(state, ensure_ascii=False, indent=2))
            entries = list(plan['updates'].items())
            for start in range(0, len(entries), 200):
                result = lark(args.lark_cli, '+record-batch-update', '--base-token', base, '--table-id', table,
                              '--json', json.dumps({'update_records': dict(entries[start:start + 200])}, ensure_ascii=False))
                if result.get('ignored_fields'):
                    raise ValidationError('Feishu ignored the code field; stop and read back')
        current = fetch_records(args.lark_cli, base, table, names)
        atomic_write(journal / 'after.json', json.dumps(current, ensure_ascii=False, indent=2))
        verify_result(projects, current, plan['updates'])
        state.pop('pending', None)
        state.update(projects=plan['state'], lastSuccess=now.isoformat(), sourceFingerprint=fingerprint(source['records']))
        atomic_write(args.state, json.dumps(state, ensure_ascii=False, indent=2))
        if args.page.read_text(encoding='utf-8') != original:
            raise ValidationError('Codes verified in Feishu, but local page changed; page was not overwritten')
        merged = merge_project_links(read_rows(original)[0], lands, current)
        atomic_write(args.page, embed_rows(original, merged))
    summary = {'status': 'success', 'applied': args.apply, 'updatedProjects': len(plan['updates']),
               'projectRecords': len(projects), 'decisions': dict(Counter(i['decision'] for i in plan['items'])),
               'sourceDataset': '6404', 'sourceRetrievedAt': source['retrievedAt'],
               'projectUrls': sum(bool(p['projectCode']) for row in merged for p in row['linkedProjects']),
               'journal': str(journal), 'completedAt': datetime.now(TZ).isoformat()}
    for path in (journal / 'report.json', args.report_dir / 'latest.json'):
        atomic_write(path, json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--base-url', default=BASE_URL)
    parser.add_argument('--lark-cli', default='lark-cli')
    parser.add_argument('--page', type=Path, default=ROOT / DASHBOARD)
    parser.add_argument('--state', type=Path, default=ROOT / 'reports/land-project-codes/state.json')
    parser.add_argument('--report-dir', type=Path, default=ROOT / 'reports/land-project-codes')
    parser.add_argument('--lock', type=Path, default=ROOT / 'reports/land-project-sync/state.lock')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--init-state', action='store_true', help='One-time authorized initialization only')
    args = parser.parse_args()
    args.lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.lock.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValidationError('Another land relationship/code sync is running') from error
            run(args)
    except (ValidationError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        report = {'status': 'failed', 'applied': False, 'error': str(error),
                  'completedAt': datetime.now(TZ).isoformat(),
                  'note': 'Writes may have occurred; preserve state and read back before retrying.'}
        atomic_write(args.report_dir / 'latest.json', json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)
        raise SystemExit(1) from error


if __name__ == '__main__':
    main()

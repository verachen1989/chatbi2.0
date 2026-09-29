"""Incremental, user-authenticated Feishu matching and local page refresh.

Default is dry run. --apply writes only the land directory, three relationship
fields and local HTML; it never pushes git or changes the public website.
"""
import argparse
import fcntl
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from import_land_tracker_project_links import (MATCH_FIELDS, ROOT, DASHBOARD, ValidationError,
    atomic_write, code_key, embed_rows, fetch_records, fingerprint, lark, merge_project_links,
    parse_date, plan_automatic_links, read_rows)
from update_land_tracker import validate_rows

BASE_URL = 'https://gtportal.feishu.cn/wiki/LGnPwSHp5iNB05kzvwScNxwHn7e?table=tbl9AoHrPiNRwSbn'
LAND_FIELDS = ('地块编号', '地块名称', '行政区', '成交时间', '官方来源')
TZ = ZoneInfo('Asia/Shanghai')


def validate_freshness(evidence, now):
    checked = datetime.fromisoformat(evidence['checkedAt'])
    if (checked.tzinfo is None or evidence.get('collectionMode') not in ('live', 'live_checkpoint')
            or not -timedelta(minutes=5) <= now - checked <= timedelta(hours=48)):
        raise ValidationError('Official snapshot is stale, future-dated or not a successful live collection; no writes')


def load_source(root, refresh, report_dir):
    if not refresh:
        return ((root / DASHBOARD).read_text(encoding='utf-8'),
                json.loads((root / 'data/land_tracker_enrichment.json').read_text()),
                json.loads((root / 'data/land_tracker_sources.json').read_text()), 'local')
    session = requests.Session()
    session.mount('https://', HTTPAdapter(max_retries=Retry(total=2, backoff_factor=1,
                  status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET'])))
    response = session.get('https://api.github.com/repos/verachen1989/chatbi2.0/commits/main', timeout=30)
    response.raise_for_status()
    sha = response.json()['sha']
    if len(sha) != 40 or any(c not in '0123456789abcdef' for c in sha):
        raise ValidationError('Invalid GitHub revision')
    downloaded = []
    # Pin all three files to one revision; never mix a page and another day's evidence.
    for path in (str(DASHBOARD), 'data/land_tracker_enrichment.json', 'data/land_tracker_sources.json'):
        response = session.get(f'https://raw.githubusercontent.com/verachen1989/chatbi2.0/{sha}/{path}', timeout=60)
        response.raise_for_status()
        response.encoding = 'utf-8'
        atomic_write(report_dir / ('source-' + Path(path).name), response.text)
        downloaded.append(response.text)
    return downloaded[0], json.loads(downloaded[1]), json.loads(downloaded[2]), sha


def directory_changes(rows, lands, sources):
    by_code = {code_key(row['地块编号']): row for row in lands}
    if len(by_code) != len(lands):
        raise ValidationError('Duplicate land codes in Feishu directory')
    create, updates = [], {}
    for row in rows:
        key = code_key(row['landCode'])
        values = {'地块编号': row['landCode'], '地块名称': row['landName'], '行政区': row['district'],
                  '成交时间': parse_date(row['dealDate']).strftime('%Y-%m-%d 00:00')}
        source = sources.get('records', {}).get(key, {}).get('url')
        if source:
            values['官方来源'] = source
        existing = by_code.get(key)
        if not existing:
            create.append(values)
            continue
        changed = {}
        for field, value in values.items():
            current = existing.get(field) or ''
            if field == '成交时间' and current:
                current = datetime.fromisoformat(current).astimezone(TZ).strftime('%Y-%m-%d %H:%M')
            elif field == '地块编号':
                current, value = code_key(current), code_key(value)
            elif field == '官方来源' and value in current:
                continue
            if current != value:
                changed[field] = value
        if changed:
            updates[existing['record_id']] = changed
    return create, updates


def assert_unchanged(before, current):
    if fingerprint(sorted(before, key=lambda r: r['record_id'])) != fingerprint(sorted(current, key=lambda r: r['record_id'])):
        raise ValidationError('Feishu was edited during matching; retry before writing')


def comparable(value):
    if isinstance(value, list) and all(isinstance(item, dict) and 'id' in item for item in value):
        return sorted(item['id'] for item in value)
    return value


def verify_updates(records, updates):
    indexed = {r['record_id']: r for r in records}
    for record_id, fields in updates.items():
        for field, expected in fields.items():
            actual = indexed.get(record_id, {}).get(field)
            if field == '成交时间' and actual:
                actual = datetime.fromisoformat(actual).astimezone(TZ).strftime('%Y-%m-%d %H:%M')
            if (comparable(actual) != comparable(expected)
                    and not (field == '官方来源' and expected in (actual or ''))):
                raise ValidationError(f'Feishu write readback differs: {record_id} / {field}; page not updated')


def write_batch(cli, base, table, updates):
    entries = list(updates.items())
    for start in range(0, len(entries), 200):
        result = lark(cli, '+record-batch-update', '--base-token', base, '--table-id', table,
                      '--json', json.dumps({'update_records': dict(entries[start:start + 200])}, ensure_ascii=False))
        if result.get('ignored_fields'):
            raise ValidationError('Feishu ignored fields; stop and inspect the write journal')


def validate_schema(cli, base, table, land_table):
    fields = lark(cli, '+field-list', '--base-token', base, '--table-id', table)['fields']
    by_name = {f['name']: f for f in fields}
    if any(name not in by_name for name in MATCH_FIELDS):
        raise ValidationError('Project schema changed')
    if (by_name['关联地块'].get('type') != 'link' or by_name['关联地块'].get('link_table') != land_table
            or by_name['地块关联状态'].get('type') != 'select'
            or by_name['地块关联状态'].get('multiple') is not False
            or not {'未关联', '需人工确认', '已确认'} <= {o['name'] for o in by_name['地块关联状态'].get('options', [])}
            or any(by_name[name].get('type') != 'text' for name in MATCH_FIELDS if name not in ('关联地块', '地块关联状态'))):
        raise ValidationError('Project relationship fields changed')
    fields = lark(cli, '+field-list', '--base-token', base, '--table-id', land_table)['fields']
    by_name = {f['name']: f for f in fields}
    if any(by_name.get(name, {}).get('type') != ('datetime' if name == '成交时间' else 'text') for name in LAND_FIELDS):
        raise ValidationError('Land directory fields changed')


def run(args):
    now = datetime.now(TZ)
    journal = args.report_dir / now.strftime('%Y%m%d-%H%M%S-%f')
    journal.mkdir(parents=True, exist_ok=False)
    original = args.page.read_text(encoding='utf-8')
    source, evidence, sources, revision = load_source(ROOT, args.refresh_source, journal)
    validate_freshness(evidence, now)
    rows = read_rows(source)[0]
    validate_rows(rows, now.date())
    local_rows = read_rows(original)[0]
    if not {code_key(r['landCode']) for r in local_rows} <= {code_key(r['landCode']) for r in rows}:
        raise ValidationError('Source snapshot would remove existing lands; stopped')
    if any(code_key(r['landCode']) not in evidence.get('records', {}) for r in rows):
        raise ValidationError('Official evidence snapshot is incomplete')
    resolved = lark(args.lark_cli, '+url-resolve', '--url', args.base_url)
    if resolved.get('block_type') != 'table':
        raise ValidationError('Project URL must select a table')
    base, table = resolved['base_token'], resolved['table_id']
    relation = lark(args.lark_cli, '+field-get', '--base-token', base, '--table-id', table, '--field-id', '关联地块')['field']
    land_table = relation.get('link_table')
    if not land_table:
        raise ValidationError('Missing land directory link')
    validate_schema(args.lark_cli, base, table, land_table)
    lands = fetch_records(args.lark_cli, base, land_table, LAND_FIELDS)
    projects = fetch_records(args.lark_cli, base, table, MATCH_FIELDS)
    state = json.loads(args.state.read_text(encoding='utf-8')) if args.state.exists() else {}
    context = {'base': base, 'table': table, 'landTable': land_table}
    if state and state.get('context') != context:
        raise ValidationError('State belongs to another Feishu table')
    create, updates = directory_changes(rows, lands, sources)
    atomic_write(journal / 'before.json', json.dumps({'lands': lands, 'projects': projects, 'state': state}, ensure_ascii=False, indent=2))
    atomic_write(journal / 'directory-plan.json', json.dumps({'create': create, 'updates': updates}, ensure_ascii=False, indent=2))
    if args.apply:
        assert_unchanged(lands, fetch_records(args.lark_cli, base, land_table, LAND_FIELDS))
        write_batch(args.lark_cli, base, land_table, updates)
        for start in range(0, len(create), 200):
            result = lark(args.lark_cli, '+record-batch-create', '--base-token', base, '--table-id', land_table,
                          '--json', json.dumps({'create_records': create[start:start + 200]}, ensure_ascii=False))
            if result.get('ignored_fields') or len(result.get('record_id_list', [])) != len(create[start:start + 200]):
                raise ValidationError('Directory creation result uncertain; read back before retrying')
        lands = fetch_records(args.lark_cli, base, land_table, LAND_FIELDS)
        if any(directory_changes(rows, lands, sources)):
            raise ValidationError('Directory readback differs')
    elif create:
        # Preview IDs never leave a dry run and are never written to Feishu.
        lands += [dict(values, record_id='preview-' + str(i)) for i, values in enumerate(create)]
    plan = plan_automatic_links(rows, lands, projects, evidence, state.get('projects', {}))
    projected = [dict(p, **plan['updates'].get(p['record_id'], {})) for p in projects]
    merged = merge_project_links(rows, lands, projected)
    atomic_write(journal / 'matching-plan.json', json.dumps(plan, ensure_ascii=False, indent=2))
    atomic_write(journal / 'proposed.html', embed_rows(original, merged))
    if args.apply:
        assert_unchanged(projects, fetch_records(args.lark_cli, base, table, MATCH_FIELDS))
        write_batch(args.lark_cli, base, table, plan['updates'])
        current = fetch_records(args.lark_cli, base, table, MATCH_FIELDS)
        verify_updates(current, plan['updates'])
        merged = merge_project_links(rows, lands, current)
        if args.page.read_text(encoding='utf-8') != original:
            raise ValidationError('Local page changed during sync; stop before replacing it')
        atomic_write(args.page, embed_rows(original, merged))
        atomic_write(args.state, json.dumps({'context': context, 'projects': plan['state'], 'lastSuccess': now.isoformat()},
                                           ensure_ascii=False, indent=2))
    else:
        current = projected
    summary = {'applied': args.apply, 'sourceRevision': revision, 'sourceCheckedAt': evidence['checkedAt'],
               'lands': len(rows), 'projectRecords': len(projects), 'createdLands': len(create),
               'updatedProjects': len(plan['updates']), 'decisions': dict(Counter(i['decision'] for i in plan['items'])),
               'confirmedProjects': sum(p.get('地块关联状态') == ['已确认'] for p in current),
               'reviewProjects': sum(p.get('地块关联状态') == ['需人工确认'] for p in current),
               'linkedLands': sum(bool(r['linkedProjects']) for r in merged),
               'projectUrls': sum(bool(p['projectCode']) for r in merged for p in r['linkedProjects']),
               'journal': str(journal), 'completedAt': datetime.now(TZ).isoformat()}
    atomic_write(journal / 'report.json', json.dumps(summary, ensure_ascii=False, indent=2))
    atomic_write(args.report_dir / 'latest.json', json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default=BASE_URL)
    parser.add_argument('--lark-cli', default='lark-cli')
    parser.add_argument('--page', type=Path, default=ROOT / DASHBOARD)
    parser.add_argument('--state', type=Path, default=ROOT / 'reports/land-project-sync/state.json')
    parser.add_argument('--report-dir', type=Path, default=ROOT / 'reports/land-project-sync')
    parser.add_argument('--refresh-source', action='store_true', help='Read latest successful collection from one pinned GitHub revision')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValidationError('Another relationship sync is running') from error
        run(args)


if __name__ == '__main__':
    main()

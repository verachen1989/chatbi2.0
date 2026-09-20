"""Publish a validated public feed and generate a native Feishu pull workflow.

The feed contains public source fields only. Credentials and manual Base data
never enter this module. Empty optional values are omitted from update patches.
"""
import argparse
import hashlib
import json
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from update_land_tracker import (ROOT, DASHBOARD, ValidationError, atomic_write,
                                 code_key, parse_date, read_rows, validate_rows)

TZ = ZoneInfo('Asia/Shanghai')
FEED_PATH = DASHBOARD.parent / 'feishu-feed.json'
FEED_URL = 'https://verachen1989.github.io/chatbi2.0/' + str(FEED_PATH)
CORE = {'landCode': '地块编号', 'seq': '序号', 'landName': '地块名称',
        'district': '行政区', 'dealDate': '成交时间', 'bidder': '竞得单位',
        'amount': '成交总金额（亿元）', 'floorPrice': '综合楼面价（元/㎡）'}
OPTIONAL = {'projectName': '项目名称', 'plate': '板块', 'brand': '所属地产品牌',
            'planningPermit': '建设工程规划许可证', 'constructionPlan': '建设方案',
            'constructionPermit': '施工证时间', 'firstPresale': '首个预售证',
            'firstCompletion': '首个竣工备案', 'sources': '官方来源'}
DATES = {'dealDate', 'planningPermit', 'constructionPlan', 'constructionPermit',
         'firstPresale', 'firstCompletion'}


def milliseconds(value):
    return int(value.timestamp() * 1000)


def date_value(value):
    return milliseconds(datetime.combine(parse_date(value), time(), TZ))


def evidence_text(row, sources):
    links, notes, confirmed, legacy = [], [], [], []
    transaction = sources.get('records', {}).get(code_key(row['landCode']), {})
    if transaction.get('url'):
        links.append(f"[成交公告]({transaction['url']})")
    for field in ('projectName', 'brand', *sorted(DATES - {'dealDate'})):
        if not row.get(field):
            continue
        proof = row.get('fieldEvidence', {}).get(field, {})
        label = OPTIONAL[field]
        if proof.get('status') in ('confirmed', 'alias_confirmed') and proof.get('url'):
            url = proof['url']
            if field == 'planningPermit':
                url = 'https://yewu.ghzrzyw.beijing.gov.cn/gwxxfb/cxghjsgcgh/jsgcgh.html'
            title = label + (' ' + proof['permit'] if proof.get('permit') else '')
            links.append(f'[{title}]({url})')
            if proof['status'] == 'confirmed':
                confirmed.append(label)
            else:
                notes.append('官方关联名称：' + proof.get('candidate', '') +
                             '；保留原项目名称，二者对应关系未单独核实。')
        else:
            legacy.append(label)
    if confirmed:
        notes.insert(0, '已匹配官方记录：' + '、'.join(confirmed) + '。')
    if legacy:
        notes.append('历史保留、尚未重新核实：' + '、'.join(legacy) + '。')
    notes.append('空白表示尚未收录，不等于尚未取证。')
    return '\n'.join(links), '\n'.join(notes)


def build_feed(rows, evidence, sources, now):
    if not rows or len(rows) > 1000:
        raise ValidationError('Feed must contain 1..1000 land records')
    validate_rows(rows, now.astimezone(TZ).date())
    checked = datetime.fromisoformat(evidence['checkedAt'])
    if (checked.tzinfo is None or evidence.get('collectionMode') != 'live'
            or checked > now + timedelta(minutes=5)
            or checked < now - timedelta(hours=48)):
        raise ValidationError('Feed requires a recent successful live collection')
    result = []
    for seq, row in enumerate(sorted(rows, key=lambda r: (parse_date(r['dealDate']),
                                                        code_key(r['landCode'])), reverse=True), 1):
        item = {key: row[key] for key in CORE}
        item.update(seq=seq, dealDate=date_value(row['dealDate']))
        item['dealDateText'] = parse_date(row['dealDate']).strftime('%Y/%m/%d 00:00:00')
        source_text, item['notes'] = evidence_text(row, sources)
        item['patches'] = []
        for key in OPTIONAL:
            value = source_text if key == 'sources' else row.get(key)
            if value is None or value == '':
                continue
            item['patches'].append({'field': key,
                                    'textValue': '' if key in DATES else value,
                                    'dateText': (parse_date(value).strftime('%Y/%m/%d 00:00:00')
                                                 if key in DATES else item['dealDateText']),
                                    'dateValue': date_value(value) if key in DATES else 0})
        result.append(item)
    feed = {'schemaVersion': 1, 'status': 'ready', 'checkedAt': milliseconds(checked),
            'checkedAtText': checked.isoformat(timespec='seconds'),
            'checkedAtLocal': checked.astimezone(TZ).strftime('%Y/%m/%d %H:%M:%S'),
            'expiresAt': milliseconds(checked + timedelta(hours=48)),
            'recordCount': len(result), 'rows': result,
            'updates': [dict(patch, landCode=row['landCode'])
                        for row in result for patch in row['patches']]}
    feed['updateCount'] = len(feed['updates'])
    if feed['updateCount'] > 1000:
        raise ValidationError('Native Feishu loop supports at most 1000 optional updates; split batches first')
    encoded = json.dumps(feed, ensure_ascii=False, sort_keys=True, allow_nan=False)
    feed['snapshotId'] = hashlib.sha256(encoded.encode()).hexdigest()
    return feed


def value(kind, text):
    return {'value_type': kind, 'value': text}


def condition(path, operator, right=None):
    clause = {'left_value': value('ref', path), 'operator': operator}
    if right is not None:
        clause['right_value'] = [right]
    return clause


def group(*clauses):
    return {'conjunction': 'or', 'conditions': [
        {'conjunction': 'and', 'conditions': list(clauses)}]}


def build_workflow(feed, table_name, owner_open_id, start_time, state_expiry_id,
                   date_field, checked_field,
                   state_table='同步状态'):
    """Disabled bootstrap, with a closed schema gate until UI date formulas bind.

    CLI 1.0.86 cannot serialize native workflow formulas. Bind the three date
    conversion actions and the expiry formula in Feishu, then change the schema
    gate from -1 to 1. Never enable this bootstrap as a finished workflow.
    """
    steps = []

    def step(identifier, kind, title, data, next_id=None, links=None):
        node = dict(id=identifier, type=kind, title=title, data=data, next=next_id)
        if links is not None:
            node['children'] = {'links': links}
        steps.append(node)

    def branch(identifier, title, clauses, yes, no, next_id=None):
        step(identifier, 'IfElseBranch', title, {'condition': group(*clauses)}, next_id,
             [{'kind': 'if_true', 'to': yes}, {'kind': 'if_false', 'to': no}])

    def notify(identifier, message):
        step(identifier, 'LarkMessageAction', message, {
            'receiver': [value('user', {'id': owner_open_id})], 'send_to_everyone': False,
            'title': [value('text', '地块跟踪同步异常')],
            'content': [value('text', message)], 'btn_list': []})

    def convert_date(prefix, source_ref, next_id):
        title = {'source_dates': '核验时间', 'deal_date': '成交时间', 'patch_date': '证照节点'}[prefix]
        step(prefix, 'SetRecordAction', '日期转换：' + title + '（待绑定）', {
            'table_name': state_table, 'ref_info': {'step_id': 'state_start'},
            'field_values': [{'field_name': '日期转换输入', 'value': [value('ref', source_ref)]},
                             {'field_name': '日期转换结果', 'value': [value('date', 'now')]}]}, next_id)

    lookup = {'conjunction': 'and', 'conditions': [
        {'field_name': '地块编号', 'operator': 'is',
         'value': [value('ref', '$.rows.item.landCode')]}]}
    core_fields = [{'field_name': name, 'value': [value('ref',
                   '$.deal_date.' + date_field if key == 'dealDate'
                   else '$.rows.item.' + key)]}
                   for key, name in CORE.items()]
    core_fields += [
        {'field_name': '核验说明', 'value': [value('ref', '$.rows.item.notes')]},
        {'field_name': '证照核验时间', 'value': [value('ref', '$.state_loaded.' + checked_field)]},
        {'field_name': '同步时间', 'value': [value('date', 'now')]}]
    patch_lookup = {'conjunction': 'and', 'conditions': [
        {'field_name': '地块编号', 'operator': 'is',
         'value': [value('ref', '$.patches.item.landCode')]}]}
    sample = {key: val for key, val in feed.items() if key not in ('rows', 'updates')}
    sample['rows'] = [dict(feed['rows'][0])]
    # A concrete sample declares every possible HTTP/Loop output type.
    sample['rows'][0]['patches'] = [dict(field='projectName', textValue='项目名称',
                                       dateText=feed['checkedAtLocal'],
                                       dateValue=feed['checkedAt'])]
    sample['updates'] = [dict(sample['rows'][0]['patches'][0],
                              landCode=feed['rows'][0]['landCode'])]
    step('timer', 'TimerTrigger', '每天15:00（北京时间）',
         {'rule': 'DAILY', 'start_time': start_time, 'is_never_end': True}, 'state_start')
    step('state_start', 'AddRecordAction', '记录本次同步开始', {
        'table_name': state_table, 'field_values': [
            {'field_name': '任务', 'value': [value('text', '地块跟踪每日同步')]},
            {'field_name': '状态', 'value': [value('text', '执行中，未结束请检查工作流日志')]},
            {'field_name': '开始时间', 'value': [value('date', 'now')]}]}, 'http')
    step('http', 'HTTPClientAction', '读取官方采集校验后的公开快照', {
        'method': 'GET', 'url': [value('text', FEED_URL)], 'body_type': 'none',
        'response_type': 'json', 'response_value': json.dumps(sample, ensure_ascii=False)}, 'valid')
    branch('valid', '检查接口、版本和数量', [
        condition('$.http.status_code', 'is', value('number', 200)),
        condition('$.http.body.schemaVersion', 'is', value('number', -1)),
        condition('$.http.body.status', 'is', value('text', 'ready')),
        condition('$.http.body.recordCount', 'isGreater', value('number', 0)),
        condition('$.http.body.recordCount', 'isLessEqual', value('number', 1000)),
        condition('$.http.body.updateCount', 'isLessEqual', value('number', 1000)),
        condition('$.http.body.rows', 'isNotEmpty')], 'source_dates', 'state_invalid_version')
    convert_date('source_dates', '$.http.body.checkedAtLocal', 'state_loaded')
    step('state_loaded', 'SetRecordAction', '记录快照核验时间及有效期', {
        'table_name': state_table, 'ref_info': {'step_id': 'state_start'},
        'field_values': [
            {'field_name': '数据核验时间', 'value': [value('ref', '$.source_dates.' + date_field)]},
            {'field_name': '数据有效期', 'value': [value('date', 'now')]},
            {'field_name': '地块数', 'value': [value('ref', '$.http.body.recordCount')]},
            {'field_name': '快照标识', 'value': [value('ref', '$.http.body.snapshotId')]}]}, 'fresh')
    branch('fresh', '只接收48小时内成功核验的数据',
           [condition('$.state_loaded.' + state_expiry_id, 'isGreater', value('ref', '$.timer.scheduleTime'))],
           'rows', 'state_invalid_expiry')
    for reason in ('version', 'expiry'):
        step('state_invalid_' + reason, 'SetRecordAction', '记录数据异常，不更新地块表', {
            'table_name': state_table, 'ref_info': {'step_id': 'state_start'},
            'field_values': [
                {'field_name': '状态', 'value': [value('text', '数据异常或过期，已停止同步')]},
                {'field_name': '结束时间', 'value': [value('date', 'now')]}]}, 'invalid_' + reason)
        notify('invalid_' + reason, '数据接口异常或超过48小时未成功核验，本次未更新地块表，请检查上游采集。')
    step('rows', 'Loop', '逐宗地块同步（失败即停止）',
         {'loop_mode': 'end', 'max_loop_times': 1000,
          'data': [value('ref', '$.http.body.rows')]},
         'patches',
         links=[{'kind': 'loop_start', 'to': 'deal_date'}])
    convert_date('deal_date', '$.rows.item.dealDateText', 'find')
    step('state_end', 'SetRecordAction', '记录处理结束', {
        'table_name': state_table, 'ref_info': {'step_id': 'state_start'},
        'field_values': [
            {'field_name': '状态', 'value': [value('text', '处理结束；重复编号如有则已单独通知')]},
            {'field_name': '结束时间', 'value': [value('date', 'now')]}]})
    step('find', 'FindRecordAction', '按地块编号查找已有记录', {
        'table_name': table_name, 'field_names': ['地块编号'],
        'should_proceed_when_no_results': True, 'filter_info': lookup}, 'upsert')
    notify('duplicate', '地块编号存在重复记录，已跳过该宗地更新，请在地块跟踪明细中检查重复编号。')
    notify('unexpected', '同步分支出现未识别的返回值，请检查本次运行日志。')
    step('upsert', 'SwitchBranch', '新增、唯一更新或重复提醒', {
        'mode': 'exclusive', 'no_match_action': 'classifyToOther',
        'child_branch_list': [
            {'name': name, 'condition': group(condition('$.find.recordNum', operator, value('number', count)))}
            for name, operator, count in [('新增', 'is', 0), ('更新', 'is', 1), ('重复', 'isGreater', 1)]]},
         None, [{'kind': 'case', 'label': label, 'desc': name, 'to': target}
                      for label, name, target in [('new', '新增', 'add'), ('existing', '更新', 'update'),
                                                   ('duplicate', '重复', 'duplicate'),
                                                   ('other', '其他', 'unexpected')]])
    step('eligible', 'SwitchBranch', '仅对唯一匹配同步后续字段', {
        'mode': 'exclusive', 'no_match_action': 'classifyToOther',
        'child_branch_list': [{'name': '匹配正常', 'condition': group(
            condition('$.find_patch.recordNum', 'is', value('number', 1)))},
            {'name': '重复记录', 'condition': group(
                condition('$.find_patch.recordNum', 'isGreater', value('number', 1)))}]},
         links=[{'kind': 'case', 'label': 'valid', 'desc': '匹配正常', 'to': 'patch_date'},
                {'kind': 'case', 'label': 'duplicate', 'desc': '重复记录', 'to': 'skip_patch'},
                {'kind': 'case', 'label': 'other', 'desc': '其他', 'to': 'missing_patch'}])
    step('skip_patch', 'Delay', '跳过重复地块（基础信息阶段已通知）', {'duration': 1})
    notify('missing_patch', '未找到地块基础记录，已跳过后续字段更新，请检查运行日志。')
    step('add', 'AddRecordAction', '新增地块基础信息',
         {'table_name': table_name, 'field_values': core_fields})
    step('update', 'SetRecordAction', '更新地块基础信息', {
        'table_name': table_name, 'max_set_record_num': 1,
        'filter_info': lookup, 'field_values': core_fields})
    step('patches', 'Loop', '仅同步非空项目、品牌、证照和来源',
         {'loop_mode': 'end', 'max_loop_times': 1000,
          'data': [value('ref', '$.http.body.updates')]}, 'state_end',
         [{'kind': 'loop_start', 'to': 'find_patch'}])
    step('find_patch', 'FindRecordAction', '更新前再次确认地块编号唯一', {
        'table_name': table_name, 'field_names': ['地块编号'],
        'should_proceed_when_no_results': True, 'filter_info': patch_lookup}, 'eligible')
    convert_date('patch_date', '$.patches.item.dateText', 'patch_field')
    step('patch_field', 'SwitchBranch', '按字段类型写入，空值不清空已有内容', {
        'mode': 'exclusive', 'no_match_action': 'classifyToOther',
        'child_branch_list': [
            {'name': name, 'condition': group(condition('$.patches.item.field', 'is', value('text', key)))}
            for key, name in OPTIONAL.items()]},
         links=[{'kind': 'case', 'label': key, 'desc': name, 'to': 'patch_' + key}
                for key, name in OPTIONAL.items()] +
               [{'kind': 'case', 'label': 'other', 'desc': '其他', 'to': 'unexpected_patch'}])
    notify('unexpected_patch', '同步数据出现未识别字段，请检查本次运行日志。')
    for key, name in OPTIONAL.items():
        step('patch_' + key, 'SetRecordAction', '更新' + name, {
            'table_name': table_name, 'max_set_record_num': 1, 'filter_info': patch_lookup,
            'field_values': [{'field_name': name, 'value': [value('ref',
                             '$.patch_date.' + date_field if key in DATES
                             else '$.patches.item.textValue')]}]})
    return {'client_token': str(uuid.uuid4()), 'title': '地块跟踪每日自动同步（15:00）', 'steps': steps}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--workflow-output', type=Path)
    parser.add_argument('--owner-open-id')
    parser.add_argument('--table-name', default='地块跟踪明细')
    parser.add_argument('--start-time')
    parser.add_argument('--state-expiry-field')
    parser.add_argument('--date-field')
    parser.add_argument('--checked-field')
    args = parser.parse_args()
    now = datetime.now(TZ)
    rows = read_rows((args.root / DASHBOARD).read_text(encoding='utf-8'))[0]
    evidence = json.loads((args.root / 'data/land_tracker_enrichment.json').read_text())
    sources = json.loads((args.root / 'data/land_tracker_sources.json').read_text())
    feed = build_feed(rows, evidence, sources, now)
    atomic_write(args.root / FEED_PATH, json.dumps(feed, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    if args.workflow_output:
        if not all((args.owner_open_id, args.start_time, args.state_expiry_field,
                    args.date_field, args.checked_field)):
            parser.error('Workflow requires owner, start time, and actual state/date field IDs')
        workflow = build_workflow(feed, args.table_name, args.owner_open_id, args.start_time,
                                  args.state_expiry_field, args.date_field, args.checked_field)
        atomic_write(args.workflow_output, json.dumps(workflow, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'rows': feed['recordCount'], 'checkedAt': feed['checkedAtText'],
                      'snapshotId': feed['snapshotId'], 'feed': str(args.root / FEED_PATH)}))


if __name__ == '__main__':
    main()

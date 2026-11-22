"""人工复核工作量台账与勘误流。

课程组只把教学合成记录的课程编号、提交编号、日期、版本号与现有 v1 报告的
五个整数摘要交给本模块；模块不打开完整报告、不读源字符串、片段或切分规则。

台账把每个提交的已接纳最高版本、当前 `(course_id, day)` 分档桶和不可改写的
勘误流放在同一个持久状态里，并以一次原子替换对外生效，因此：
- 读者永远看到某个完整已提交状态（桶表与游标互相对应）；
- 写者中途终止时，新版本、桶表与勘误要么全部可见，要么全部不可见；
- 新进程直接接着上次的有效台账工作，不依赖原输入批次。

工作量口径沿用盲测编排的摘要接入校验：``fragments`` 为 1、2–7、8 以上
分别是 ``single``/``light``/``heavy``，对应复核工作量点数 1/3/6。
点数只用于课程排班，不是工时或薪资。
"""
import datetime
import json
import os
from pathlib import Path
import re
import tempfile

from .checks import array, fields, ident, integer
from .holdout import TIERS, validate_summary
from .io import constant, pairs

SCHEMA_VERSION = 1
LEDGER_FORMAT = 1
MAX_BATCH = 8192
MAX_REVISION = 2147483647
MAX_LIMIT = 100
# 每批至多 8192 条记录，每条最多影响新旧两个桶；同一桶只出现一次。
MAX_DELTAS_PER_ENTRY = MAX_BATCH * 2
POINTS = (1, 3, 6)
BUCKET_SEPARATOR = '|'
# 临时文件名带上台账文件名，多个台账共用一个目录时互不干扰。
STAGING_SUFFIX = '.ledger-staging-'
_DAY_PATTERN = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_PUT_FIELDS = ('submission_id', 'revision', 'op', 'course_id', 'day', 'summary')
_CANCEL_FIELDS = ('submission_id', 'revision', 'op')


class LedgerError(ValueError):
    """台账的业务/结构错误；CLI 统一以退出码 2 报告。"""


# --- 输入校验 -------------------------------------------------------------

def _day(value):
    """合法公历日期，严格 ``YYYY-MM-DD``（四位年、两位月日）。"""
    if not isinstance(value, str) or _DAY_PATTERN.fullmatch(value) is None:
        raise LedgerError('day must be a strict YYYY-MM-DD date')
    try:
        datetime.date(int(value[:4]), int(value[5:7]), int(value[8:10]))
    except ValueError as exc:
        raise LedgerError(f'invalid calendar day: {value}') from exc
    return value


def _revision(value):
    return integer(value, 1, MAX_REVISION)


def _validate_record(item):
    """校验单条记录并返回内部规范形式；结构/值错误立即拒绝整批。"""
    if not isinstance(item, dict):
        raise LedgerError('each record must be an object')
    op = item.get('op')
    if op == 'put':
        fields(item, _PUT_FIELDS)
        submission_id = ident(item['submission_id'])
        revision = _revision(item['revision'])
        course_id = ident(item['course_id'])
        day = _day(item['day'])
        try:
            tier = validate_summary(item['summary'])
        except ValueError as exc:
            raise LedgerError(
                f'invalid summary for {submission_id}: {exc}') from exc
        summary = {key: item['summary'][key] for key in (
            'bases', 'fragment_bases', 'fragments', 'unique_cuts',
            'recognition_hits')}
        return {'submission_id': submission_id, 'revision': revision,
                'op': 'put', 'course_id': course_id, 'day': day,
                'summary': summary, 'tier': tier}
    if op == 'cancel':
        fields(item, _CANCEL_FIELDS)
        submission_id = ident(item['submission_id'])
        revision = _revision(item['revision'])
        return {'submission_id': submission_id, 'revision': revision,
                'op': 'cancel'}
    raise LedgerError("op must be 'put' or 'cancel'")


def validate_batch(data):
    """校验整批记录（含最终会过时的记录），返回规范记录表。"""
    fields(data, ('records',))
    items = array(data['records'], 1, MAX_BATCH)
    records = []
    seen = set()
    for item in items:
        record = _validate_record(item)
        submission_id = record['submission_id']
        if submission_id in seen:
            raise LedgerError(f'duplicate submission_id in batch: {submission_id}')
        seen.add(submission_id)
        records.append(record)
    return records


# --- 持久状态 -------------------------------------------------------------

def _empty_state():
    return {'records': {}, 'buckets': {}, 'entries': []}


def _int_triple(value):
    if not isinstance(value, list) or len(value) != 3:
        raise LedgerError('ledger bucket counts must be a three-integer list')
    result = []
    for item in value:
        if type(item) is not int or item < 0:
            raise LedgerError('ledger bucket counts must be non-negative integers')
        result.append(item)
    return result


def _load_state(path):
    """读取并校验台账；路径不存在视为游标 0 的空台账。"""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return _empty_state()
    except OSError as exc:
        raise LedgerError(f'cannot read ledger: {exc}') from exc
    if not raw:
        raise LedgerError('ledger file is empty')
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=constant)
    except (UnicodeError, ValueError) as exc:
        raise LedgerError(f'ledger is corrupt: {exc}') from exc
    if not isinstance(data, dict) or data.get('ledger_format') != LEDGER_FORMAT:
        raise LedgerError('unsupported ledger format')

    records = data.get('records')
    buckets = data.get('buckets')
    entries = data.get('entries')
    if not isinstance(records, dict) or not isinstance(buckets, dict) \
            or not isinstance(entries, list):
        raise LedgerError('ledger is missing records/buckets/entries')

    checked_records = {}
    for submission_id, record in records.items():
        ident(submission_id)
        if not isinstance(record, dict):
            raise LedgerError('ledger record must be an object')
        op = record.get('op')
        if op == 'put':
            fields(record, ('revision', 'op', 'course_id', 'day', 'summary'))
            revision = _revision(record['revision'])
            course_id = ident(record['course_id'])
            day = _day(record['day'])
            try:
                tier = validate_summary(record['summary'])
            except ValueError as exc:
                raise LedgerError(f'ledger record {submission_id} is corrupt: {exc}') from exc
            summary = {key: record['summary'][key] for key in (
                'bases', 'fragment_bases', 'fragments', 'unique_cuts',
                'recognition_hits')}
            checked_records[submission_id] = {
                'revision': revision, 'op': 'put', 'course_id': course_id,
                'day': day, 'summary': summary, 'tier': tier}
        elif op == 'cancel':
            fields(record, ('revision', 'op'))
            checked_records[submission_id] = {
                'revision': _revision(record['revision']), 'op': 'cancel'}
        else:
            raise LedgerError('ledger record has an unknown op')

    checked_buckets = {}
    for key, counts in buckets.items():
        if not isinstance(key, str) or BUCKET_SEPARATOR not in key:
            raise LedgerError('ledger bucket key is corrupt')
        course_id, day = key.split(BUCKET_SEPARATOR, 1)
        ident(course_id)
        _day(day)
        triple = _int_triple(counts)
        if all(value == 0 for value in triple):
            raise LedgerError('ledger must not store all-zero buckets')
        checked_buckets[key] = triple

    checked_entries = []
    for index, entry in enumerate(entries):
        fields(entry, ('cursor', 'deltas'))
        cursor = entry['cursor']
        if type(cursor) is not int or cursor != index + 1:
            raise LedgerError('ledger cursors must start at 1 and be contiguous')
        deltas = array(entry['deltas'], 1, MAX_DELTAS_PER_ENTRY)
        checked_deltas = []
        for delta in deltas:
            fields(delta, ('course_id', 'day', 'counts'))
            course_id = ident(delta['course_id'])
            day = _day(delta['day'])
            counts = delta['counts']
            if not isinstance(counts, list) or len(counts) != 3:
                raise LedgerError('ledger delta counts must be a three-integer list')
            signed = [integer(counts[i], -MAX_BATCH, MAX_BATCH) for i in range(3)]
            if all(value == 0 for value in signed):
                raise LedgerError('ledger must not store all-zero deltas')
            checked_deltas.append(
                {'course_id': course_id, 'day': day, 'counts': signed})
        checked_entries.append({'cursor': cursor, 'deltas': checked_deltas})

    return {'records': checked_records, 'buckets': checked_buckets,
            'entries': checked_entries}


def _state_payload(state):
    # tier 由 summary 推导，不写入持久表示，避免同一事实有两种写法。
    records = {
        submission_id: {key: value for key, value in record.items()
                        if key != 'tier'}
        for submission_id, record in state['records'].items()
    }
    return (json.dumps(
        {'ledger_format': LEDGER_FORMAT, 'records': records,
         'buckets': state['buckets'], 'entries': state['entries']},
        ensure_ascii=False, sort_keys=True, allow_nan=False,
        separators=(',', ':')) + '\n').encode('utf-8')


def _staging_prefix(target):
    return f'.{target.name}{STAGING_SUFFIX}'


def _clean_staging(target):
    for stale in target.parent.glob(_staging_prefix(target) + '*'):
        if stale.is_file() or stale.is_symlink():
            stale.unlink(missing_ok=True)


def _fsync_directory(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _commit(path, state, interrupt=None):
    """把完整状态一次原子替换发布；中断时旧状态保持原样。"""
    target = Path(path)
    if target.is_symlink():
        raise LedgerError('ledger store must not be a symlink')
    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    _clean_staging(target)
    payload = _state_payload(state)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                dir=directory, prefix=_staging_prefix(target),
                delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # 测试挂钩：此时完整批次已写入临时介质，但尚未对外生效。
        if interrupt is not None:
            interrupt()
        os.replace(temporary, target)
        temporary = None
        _fsync_directory(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# --- 私有表示与公开投影 ---------------------------------------------------

def _bucket_key(course_id, day):
    return f'{course_id}{BUCKET_SEPARATOR}{day}'


def _decode_key(key):
    course_id, day = key.split(BUCKET_SEPARATOR, 1)
    return course_id, day


def _same_version(left, right):
    """版本相同即比较约定字段值，而不是 JSON 字节。"""
    if left['op'] != right['op']:
        return False
    if right['op'] == 'cancel':
        return True
    return (left['course_id'] == right['course_id']
            and left['day'] == right['day']
            and left['summary'] == right['summary'])


def _points(counts):
    return sum(counts[index] * POINTS[index] for index in range(3))


def _bucket_row(course_id, day, counts):
    return {'course_id': course_id, 'day': day,
            'counts': {TIERS[index]: counts[index] for index in range(3)},
            'points': _points(counts)}


def _entry_row(entry):
    return {'cursor': entry['cursor'],
            'deltas': [_bucket_row(delta['course_id'], delta['day'],
                                   delta['counts'])
                       for delta in entry['deltas']]}


def _sorted_bucket_keys(buckets):
    return sorted(buckets, key=_decode_key)


# --- 接入 -----------------------------------------------------------------

def apply_batch(store_path, data, interrupt=None):
    """校验并接纳一个批次，返回 {'schema_version','head','emitted'}。

    ``interrupt`` 只在测试中使用：临时状态已 fsync、正式替换尚未发生时调用。
    调用方抛错即模拟写者中止，旧状态不受影响。
    """
    state = _load_state(store_path)
    records = state['records']
    buckets = state['buckets']
    entries = state['entries']

    batch = validate_batch(data)

    deltas = {}
    new_records = dict(records)
    changed = False
    for record in batch:
        submission_id = record['submission_id']
        current = records.get(submission_id)
        if current is not None:
            if record['revision'] < current['revision']:
                # 过时版本已完整校验，但不改变事实。
                continue
            if record['revision'] == current['revision']:
                if _same_version(current, record):
                    continue
                raise LedgerError(
                    f'revision {record["revision"]} of {submission_id} '
                    'conflicts with the accepted version')
        # 更高版本（或首次见到）完整替代旧状态。
        if current is not None and current['op'] == 'put':
            key = _bucket_key(current['course_id'], current['day'])
            deltas.setdefault(key, [0, 0, 0])[current['tier']] -= 1
        if record['op'] == 'put':
            key = _bucket_key(record['course_id'], record['day'])
            deltas.setdefault(key, [0, 0, 0])[record['tier']] += 1
        stored = {'revision': record['revision'], 'op': record['op']}
        if record['op'] == 'put':
            stored.update({'course_id': record['course_id'],
                           'day': record['day'],
                           'summary': record['summary']})
        new_records[submission_id] = stored
        changed = True

    nonzero = {key: value for key, value in deltas.items()
               if any(value)}
    new_buckets = {key: list(value) for key, value in buckets.items()}
    for key, delta in nonzero.items():
        bucket = new_buckets.setdefault(key, [0, 0, 0])
        for index in range(3):
            bucket[index] += delta[index]
        if any(value < 0 for value in bucket):
            raise LedgerError('ledger bucket would become negative')
        if all(value == 0 for value in bucket):
            del new_buckets[key]
    new_entries = entries
    emitted = bool(nonzero)
    if emitted:
        ordered = [{'course_id': _decode_key(key)[0],
                    'day': _decode_key(key)[1],
                    'counts': nonzero[key]}
                   for key in sorted(nonzero, key=_decode_key)]
        new_entries = entries + [{'cursor': len(entries) + 1,
                                  'deltas': ordered}]

    if changed or emitted:
        _commit(store_path, {'records': new_records, 'buckets': new_buckets,
                             'entries': new_entries}, interrupt=interrupt)
    head = len(new_entries)
    return {'schema_version': SCHEMA_VERSION, 'head': head,
            'emitted': emitted}


# --- 仪表盘接口 -----------------------------------------------------------

def snapshot(store_path):
    """返回同一已提交状态下的游标与完整桶表（不列全零桶）。"""
    state = _load_state(store_path)
    buckets = state['buckets']
    return {
        'schema_version': SCHEMA_VERSION,
        'head': len(state['entries']),
        'buckets': [_bucket_row(*_decode_key(key), buckets[key])
                    for key in _sorted_bucket_keys(buckets)],
    }


def changes(store_path, after, limit):
    """从游标 ``after`` 之后按升序返回最多 ``limit`` 条完整勘误。"""
    state = _load_state(store_path)
    entries = state['entries']
    head = len(entries)
    if type(after) is not int or after < 0:
        raise LedgerError('after must be a non-negative integer')
    if after > head:
        raise LedgerError(f'after {after} is beyond head {head}')
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise LedgerError(f'limit must be an integer 1..{MAX_LIMIT}')
    page = entries[after:after + limit]
    next_cursor = page[-1]['cursor'] if page else after
    return {
        'schema_version': SCHEMA_VERSION,
        'head': head,
        'next': next_cursor,
        'entries': [_entry_row(entry) for entry in page],
    }

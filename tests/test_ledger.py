"""人工复核工作量台账测试：验收场景、勘误流、版本规则、中断与兼容性。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from sequence_digest.io import load
from sequence_digest.ledger import (
    LedgerError, apply_batch, changes, snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
V1_SOURCE = ROOT / 'fixtures' / 'input.json'
FIXTURES = ROOT / 'fixtures' / 'ledger'
TIERS = ('single', 'light', 'heavy')


def run_cli(*args, cwd=None, env_extra=None, stdin=None):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(SRC)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, '-P', '-m', 'sequence_digest.ledger_cli', *args],
        cwd=cwd or str(ROOT), env=env, text=True,
        stdin=stdin if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def summary(fragments=1, bases=120):
    return {
        'bases': bases,
        'fragment_bases': bases,
        'fragments': fragments,
        'unique_cuts': 0,
        'recognition_hits': 0,
    }


def put(submission_id, revision, course_id, day, fragments=1, bases=120, **extra):
    record = {
        'submission_id': submission_id,
        'revision': revision,
        'op': 'put',
        'course_id': course_id,
        'day': day,
        'summary': summary(fragments, bases),
    }
    record.update(extra)
    return record


def cancel(submission_id, revision):
    return {'submission_id': submission_id, 'revision': revision, 'op': 'cancel'}


def batch(*records):
    return {'records': list(records)}


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8')
    return path


def bucket_map(snap):
    return {(row['course_id'], row['day']): row for row in snap['buckets']}


def apply_deltas(table, entry):
    """把一条勘误的有符号增量并进仪表盘桶表，去掉归零桶。"""
    for delta in entry['deltas']:
        key = (delta['course_id'], delta['day'])
        current = table.get(key, {'counts': {tier: 0 for tier in TIERS},
                                  'points': 0})
        counts = {tier: current['counts'][tier] + delta['counts'][tier]
                  for tier in TIERS}
        points = current['points'] + delta['points']
        if all(value == 0 for value in counts.values()):
            table.pop(key, None)
        else:
            table[key] = {'counts': counts, 'points': points}
    return table


def drain(store, after):
    """按 limit=1 拉完所有页，返回 (next 游标, 全部勘误)。"""
    entries = []
    cursor = after
    while True:
        page = changes(store, cursor, 1)
        if not page['entries']:
            return cursor, entries
        cursor = page['next']
        entries.extend(page['entries'])


class AcceptanceScenarioTests(unittest.TestCase):
    """验收第 1 条：初始批 + 更正批的净变动与两种拉页起点。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.store = self.work / 'store.json'

    def tearDown(self):
        self.tmp.cleanup()

    def _initial(self):
        result = apply_batch(self.store, batch(
            put('A', 1, 'blue', '2026-11-10', fragments=2),
            put('B', 1, 'red', '2026-11-10', fragments=1)))
        self.assertEqual(result, {'schema_version': 1, 'head': 1,
                                  'emitted': True})
        return result

    def test_initial_batch_creates_cursor_one(self):
        self._initial()
        snap = snapshot(self.store)
        self.assertEqual(snap['head'], 1)
        rows = bucket_map(snap)
        self.assertEqual(set(rows), {('blue', '2026-11-10'),
                                     ('red', '2026-11-10')})
        self.assertEqual(rows[('blue', '2026-11-10')]['counts'],
                         {'single': 0, 'light': 1, 'heavy': 0})
        self.assertEqual(rows[('blue', '2026-11-10')]['points'], 3)
        self.assertEqual(rows[('red', '2026-11-10')]['counts'],
                         {'single': 1, 'light': 0, 'heavy': 0})
        self.assertEqual(rows[('red', '2026-11-10')]['points'], 1)

        first = changes(self.store, 0, 10)
        self.assertEqual(first['head'], 1)
        self.assertEqual(first['next'], 1)
        self.assertEqual(len(first['entries']), 1)
        self.assertEqual(first['entries'][0]['cursor'], 1)

    def test_correction_net_delta_and_cross_bucket_cancellation(self):
        self._initial()
        result = apply_batch(self.store, batch(
            put('A', 3, 'red', '2026-11-11', fragments=9),
            put('B', 2, 'blue', '2026-11-10', fragments=2)))
        self.assertEqual(result, {'schema_version': 1, 'head': 2,
                                  'emitted': True})

        page = changes(self.store, 1, 1)
        self.assertEqual(len(page['entries']), 1)
        entry = page['entries'][0]
        self.assertEqual(entry['cursor'], 2)
        # 同一条勘误：red/10 single -1 / -1 点，red/11 heavy +1 / +6 点。
        # blue/10 的旧 A（light）与新 B（light）轧差为零，不出现。
        self.assertEqual(entry['deltas'], [
            {'course_id': 'red', 'day': '2026-11-10',
             'counts': {'single': -1, 'light': 0, 'heavy': 0}, 'points': -1},
            {'course_id': 'red', 'day': '2026-11-11',
             'counts': {'single': 0, 'light': 0, 'heavy': 1}, 'points': 6},
        ])

        snap = snapshot(self.store)
        self.assertEqual(snap['head'], 2)
        self.assertEqual(snap['buckets'], [
            {'course_id': 'blue', 'day': '2026-11-10',
             'counts': {'single': 0, 'light': 1, 'heavy': 0}, 'points': 3},
            {'course_id': 'red', 'day': '2026-11-11',
             'counts': {'single': 0, 'light': 0, 'heavy': 1}, 'points': 6},
        ])

    def test_dashboard_catches_up_from_empty_and_from_snapshot(self):
        self._initial()
        # 从游标 1 的快照起点继续：先留一份旧快照。
        mid_snapshot = snapshot(self.store)
        self.assertEqual(mid_snapshot['head'], 1)

        # 仪表盘甲：空台账起点，limit=1 逐页建立桶表。
        empty = snapshot(self.work / 'missing.json')
        self.assertEqual(empty, {'schema_version': 1, 'head': 0, 'buckets': []})
        table_a = {}
        cursor_a = 0
        # 仪表盘乙：从游标 1 的旧快照继续。
        table_b = {key: {'counts': dict(row['counts']), 'points': row['points']}
                   for key, row in bucket_map(mid_snapshot).items()}
        cursor_b = mid_snapshot['head']

        # 更正批在两份仪表盘取快照之间才写入。
        apply_batch(self.store, batch(
            put('A', 3, 'red', '2026-11-11', fragments=9),
            put('B', 2, 'blue', '2026-11-10', fragments=2)))

        while True:
            page = changes(self.store, cursor_a, 1)
            if not page['entries']:
                self.assertEqual(page['next'], cursor_a)
                break
            for entry in page['entries']:
                apply_deltas(table_a, entry)
            cursor_a = page['next']
        while True:
            page = changes(self.store, cursor_b, 1)
            if not page['entries']:
                self.assertEqual(page['next'], cursor_b)
                break
            for entry in page['entries']:
                apply_deltas(table_b, entry)
            cursor_b = page['next']

        final = snapshot(self.store)
        self.assertEqual(cursor_a, final['head'])
        self.assertEqual(cursor_b, final['head'])
        expected = bucket_map(final)
        for table in (table_a, table_b):
            self.assertEqual(set(table), set(expected))
            for key, row in expected.items():
                self.assertEqual(table[key]['counts'], row['counts'])
                self.assertEqual(table[key]['points'], row['points'])

    def test_cli_acceptance_round_trip(self):
        initial = write_json(self.work / 'initial.json',
                             load(FIXTURES / 'initial.json'))
        correction = write_json(self.work / 'correction.json',
                                load(FIXTURES / 'correction.json'))
        result = run_cli('apply', '--store', str(self.store),
                         '--source', str(initial))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {'schema_version': 1, 'head': 1, 'emitted': True})

        result = run_cli('apply', '--store', str(self.store),
                         '--source', str(correction))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {'schema_version': 1, 'head': 2, 'emitted': True})

        result = run_cli('changes', '--store', str(self.store),
                         '--after', '1', '--limit', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['next'], 2)
        self.assertEqual(payload['head'], 2)
        self.assertEqual(payload['entries'][0]['cursor'], 2)

        result = run_cli('changes', '--store', str(self.store),
                         '--after', '2', '--limit', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {'schema_version': 1, 'head': 2, 'next': 2,
                          'entries': []})


class InterruptionTests(unittest.TestCase):
    """验收第 2 条：写者停在中途、读者与后续进程只看完整状态。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.store = self.work / 'store.json'
        self.initial = write_json(self.work / 'initial.json',
                                  load(FIXTURES / 'initial.json'))
        self.correction = write_json(self.work / 'correction.json',
                                     load(FIXTURES / 'correction.json'))
        result = run_cli('apply', '--store', str(self.store),
                         '--source', str(self.initial))
        self.assertEqual(result.returncode, 0, result.stderr)

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(os.name == 'posix', 'POSIX 进程/信号语义')
    def test_reader_sees_old_state_while_writer_paused_then_continues(self):
        marker = self.work / 'paused.marker'
        env_extra = {'SEQUENCE_DIGEST_LEDGER_TEST_PAUSE_FILE': str(marker)}
        env = dict(os.environ)
        env['PYTHONPATH'] = str(SRC)
        env.update(env_extra)
        writer = subprocess.Popen(
            [sys.executable, '-P', '-m', 'sequence_digest.ledger_cli',
             'apply', '--store', str(self.store),
             '--source', str(self.correction)],
            cwd=str(ROOT), env=env, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        try:
            deadline = time.time() + 20
            while not marker.exists():
                self.assertLess(time.time(), deadline, 'writer did not reach commit point')
                self.assertIsNone(writer.poll(),
                                  'writer exited before pausing')
                time.sleep(0.02)
            self.assertIsNotNone(writer.pid)
            # 写入实现使用的工作状态/临时介质已存在，但尚未对外生效。
            self.assertTrue(any(self.work.glob('.store.json.ledger-staging-*')))

            # 另起读者：仍应是旧桶与游标 1。
            snap = run_cli('snapshot', '--store', str(self.store))
            self.assertEqual(snap.returncode, 0, snap.stderr)
            self.assertEqual(json.loads(snap.stdout), {
                'schema_version': 1, 'head': 1,
                'buckets': [
                    {'course_id': 'blue', 'day': '2026-11-10',
                     'counts': {'single': 0, 'light': 1, 'heavy': 0}, 'points': 3},
                    {'course_id': 'red', 'day': '2026-11-10',
                     'counts': {'single': 1, 'light': 0, 'heavy': 0}, 'points': 1},
                ]})
            page = run_cli('changes', '--store', str(self.store),
                           '--after', '1', '--limit', '1')
            self.assertEqual(page.returncode, 0, page.stderr)
            self.assertEqual(json.loads(page.stdout),
                             {'schema_version': 1, 'head': 1, 'next': 1,
                              'entries': []})
        finally:
            writer.terminate()
            try:
                writer.wait(timeout=10)
            except subprocess.TimeoutExpired:
                writer.kill()
                writer.wait(timeout=10)
            for stream in (writer.stdin, writer.stdout, writer.stderr):
                if stream is not None:
                    stream.close()

        # 终止后，新进程仍能正常查询旧状态。
        snap = run_cli('snapshot', '--store', str(self.store))
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertEqual(json.loads(snap.stdout)['head'], 1)
        page = run_cli('changes', '--store', str(self.store),
                       '--after', '1', '--limit', '1')
        self.assertEqual(page.returncode, 0, page.stderr)
        self.assertEqual(json.loads(page.stdout)['entries'], [])

        # 重新接入同批：恰好得到游标 2 和整条勘误。
        result = run_cli('apply', '--store', str(self.store),
                         '--source', str(self.correction))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {'schema_version': 1, 'head': 2, 'emitted': True})
        page = json.loads(run_cli('changes', '--store', str(self.store),
                                  '--after', '1', '--limit', '1').stdout)
        self.assertEqual(page['entries'], [{
            'cursor': 2,
            'deltas': [
                {'course_id': 'red', 'day': '2026-11-10',
                 'counts': {'single': -1, 'light': 0, 'heavy': 0}, 'points': -1},
                {'course_id': 'red', 'day': '2026-11-11',
                 'counts': {'single': 0, 'light': 0, 'heavy': 1}, 'points': 6},
            ]}])
        # 上次中断残留的临时介质已被清理，且不影响读取。
        self.assertFalse(any(self.work.glob('.store.json.ledger-staging-*')))

    @unittest.skipUnless(os.name == 'posix', 'POSIX 进程/信号语义')
    def test_completed_writer_delivers_page_to_old_snapshot_reader(self):
        reader_snapshot = run_cli('snapshot', '--store', str(self.store))
        self.assertEqual(json.loads(reader_snapshot.stdout)['head'], 1)
        result = run_cli('apply', '--store', str(self.store),
                         '--source', str(self.correction))
        self.assertEqual(result.returncode, 0, result.stderr)
        # 写者正常完成：以旧快照游标继续拉取不会漏掉这条勘误。
        page = json.loads(run_cli('changes', '--store', str(self.store),
                                  '--after', '1', '--limit', '1').stdout)
        self.assertEqual(page['head'], 2)
        self.assertEqual([entry['cursor'] for entry in page['entries']], [2])


class VersionAndCorrectionTests(unittest.TestCase):
    """验收第 3 条：同版本、过时版本、撤销与恢复。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.store = self.work / 'store.json'
        apply_batch(self.store, batch(
            put('A', 1, 'blue', '2026-11-10', fragments=2),
            put('B', 1, 'red', '2026-11-10', fragments=1)))
        apply_batch(self.store, batch(
            put('A', 3, 'red', '2026-11-11', fragments=9),
            put('B', 2, 'blue', '2026-11-10', fragments=2)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_tier_metadata_change_does_not_advance_cursor(self):
        # A v4 只改 recognition_hits；合法摘要、同桶同档，勘误仍停在 2。
        changed = put('A', 4, 'red', '2026-11-11', fragments=9)
        changed['summary']['recognition_hits'] = 7
        result = apply_batch(self.store, batch(changed))
        self.assertEqual(result, {'schema_version': 1, 'head': 2,
                                  'emitted': False})
        self.assertEqual(len(changes(self.store, 0, 100)['entries']), 2)

        # 合法 v3 声称把 A 搬到别处：低于已接纳 v4，不产生变动。
        stale_move = put('A', 3, 'green', '2026-12-01', fragments=1)
        result = apply_batch(self.store, batch(stale_move))
        self.assertEqual(result, {'schema_version': 1, 'head': 2,
                                  'emitted': False})
        rows = bucket_map(snapshot(self.store))
        self.assertEqual(set(rows), {('blue', '2026-11-10'),
                                     ('red', '2026-11-11')})

        # 与当前版本同为 v4 但字段不同：拒绝整批，连带保护同批本可成功的提交。
        conflicting = put('A', 4, 'green', '2026-12-01', fragments=1)
        good = put('D', 1, 'blue', '2026-11-12', fragments=1)
        with self.assertRaises(LedgerError):
            apply_batch(self.store, batch(conflicting, good))
        self.assertEqual(snapshot(self.store)['head'], 2)
        self.assertNotIn(('blue', '2026-11-12'), bucket_map(snapshot(self.store)))
        self.assertEqual(set(bucket_map(snapshot(self.store))),
                         {('blue', '2026-11-10'), ('red', '2026-11-11')})

    def test_same_version_same_values_are_no_action_regardless_of_key_order(self):
        before = snapshot(self.store)
        # 与已接纳的 B v2 完全相同，只是 JSON 字段顺序不同。
        text = ('{"records":[{"summary":{"recognition_hits":0,"unique_cuts":0,'
                '"fragments":2,"fragment_bases":120,"bases":120},'
                '"day":"2026-11-10","course_id":"blue","op":"put",'
                '"revision":2,"submission_id":"B"}]}')
        source = self.work / 'same-values.json'
        source.write_text(text, encoding='utf-8')
        result = run_cli('apply', '--store', str(self.store),
                         '--source', str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {'schema_version': 1, 'head': 2, 'emitted': False})
        self.assertEqual(snapshot(self.store), before)

    def test_stale_version_does_not_move_facts(self):
        before = snapshot(self.store)
        stale = put('A', 2, 'green', '2026-12-01', fragments=1)
        result = apply_batch(self.store, batch(stale))
        self.assertEqual(result, {'schema_version': 1, 'head': 2,
                                  'emitted': False})
        self.assertEqual(snapshot(self.store), before)

    def test_conflicting_same_version_rejects_whole_batch(self):
        head_before = snapshot(self.store)['head']
        buckets_before = snapshot(self.store)['buckets']
        conflicting = put('A', 3, 'red', '2026-11-11', fragments=9)
        conflicting['summary']['recognition_hits'] = 4
        # 同批里还有一个本可成功的提交；冲突必须连带拒绝它。
        good = put('C', 1, 'blue', '2026-11-12', fragments=1)
        with self.assertRaises(LedgerError):
            apply_batch(self.store, batch(conflicting, good))
        after = snapshot(self.store)
        self.assertEqual(after['head'], head_before)
        self.assertEqual(after['buckets'], buckets_before)
        # C 没有被接纳：它仍是首次出现，换内容按更高版本正常接纳。
        result = apply_batch(self.store, batch(
            put('C', 1, 'blue', '2026-11-12', fragments=9)))
        self.assertEqual(result, {'schema_version': 1, 'head': 3,
                                  'emitted': True})
        self.assertNotIn(('red', '2026-11-12'), bucket_map(snapshot(self.store)))

    def test_first_cancel_remembers_version_and_blocks_lower_restore(self):
        before = snapshot(self.store)
        result = apply_batch(self.store, batch(cancel('C', 5)))
        self.assertEqual(result, {'schema_version': 1, 'head': 2,
                                  'emitted': False})
        self.assertEqual(snapshot(self.store)['buckets'], before['buckets'])
        # 旧版本 put 不能把它恢复。
        result = apply_batch(self.store, batch(
            put('C', 4, 'blue', '2026-11-12', fragments=1)))
        self.assertEqual(result, {'schema_version': 1, 'head': 2,
                                  'emitted': False})
        self.assertEqual(snapshot(self.store)['buckets'], before['buckets'])
        # 更高版本 put 可以恢复。
        result = apply_batch(self.store, batch(
            put('C', 6, 'blue', '2026-11-12', fragments=1)))
        self.assertEqual(result, {'schema_version': 1, 'head': 3,
                                  'emitted': True})
        entry = changes(self.store, 2, 1)['entries'][0]
        self.assertEqual(entry['deltas'], [
            {'course_id': 'blue', 'day': '2026-11-12',
             'counts': {'single': 1, 'light': 0, 'heavy': 0}, 'points': 1}])

    def test_cancel_current_submission_emits_negative_delta(self):
        result = apply_batch(self.store, batch(cancel('A', 5)))
        self.assertEqual(result, {'schema_version': 1, 'head': 3,
                                  'emitted': True})
        entry = changes(self.store, 2, 1)['entries'][0]
        self.assertEqual(entry['deltas'], [
            {'course_id': 'red', 'day': '2026-11-11',
             'counts': {'single': 0, 'light': 0, 'heavy': -1}, 'points': -6}])
        rows = bucket_map(snapshot(self.store))
        self.assertEqual(set(rows), {('blue', '2026-11-10')})

    def test_repeated_cancel_is_idempotent(self):
        apply_batch(self.store, batch(cancel('A', 5)))
        head = snapshot(self.store)['head']
        result = apply_batch(self.store, batch(cancel('A', 5)))
        self.assertEqual(result, {'schema_version': 1, 'head': head,
                                  'emitted': False})
        # 更高版本再次撤销不产生新勘误，也不改变桶。
        result = apply_batch(self.store, batch(cancel('A', 6)))
        self.assertEqual(result, {'schema_version': 1, 'head': head,
                                  'emitted': False})


class OrderingAndNetDeltaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_batch_order_does_not_change_results(self):
        forward = batch(
            put('A', 2, 'zeta', '2026-11-11', fragments=9),
            put('B', 2, 'alpha', '2026-11-10', fragments=1),
            put('C', 1, 'alpha', '2026-11-10', fragments=2))
        backward = batch(*reversed(forward['records']))
        store_a = self.work / 'a.json'
        store_b = self.work / 'b.json'
        self.assertEqual(apply_batch(store_a, forward),
                         apply_batch(store_b, backward))
        self.assertEqual(snapshot(store_a), snapshot(store_b))
        entry_a = changes(store_a, 0, 100)['entries']
        entry_b = changes(store_b, 0, 100)['entries']
        self.assertEqual(entry_a, entry_b)
        # 桶按课程 ID、日期升序。
        days = [(d['course_id'], d['day']) for d in entry_a[0]['deltas']]
        self.assertEqual(days, [('alpha', '2026-11-10'),
                                ('zeta', '2026-11-11')])

    def test_multiple_buckets_in_one_entry_and_zero_net_versions(self):
        store = self.work / 'store.json'
        apply_batch(store, batch(
            put('A', 1, 'blue', '2026-11-10', fragments=1),
            put('B', 1, 'blue', '2026-11-10', fragments=2)))
        before = snapshot(store)
        # A 换到另一桶但档位不变：同桶减、另一桶加，blue 仍有 1 light。
        result = apply_batch(store, batch(
            put('A', 2, 'green', '2026-11-12', fragments=1)))
        self.assertEqual(result['head'], 2)
        rows = bucket_map(snapshot(store))
        self.assertEqual(rows[('blue', '2026-11-10')]['counts'],
                         {'single': 0, 'light': 1, 'heavy': 0})
        self.assertEqual(rows[('green', '2026-11-12')]['counts'],
                         {'single': 1, 'light': 0, 'heavy': 0})
        # B 也换到 green 的同一天，但档位不同：净变动仍非零，故产生勘误。
        result = apply_batch(store, batch(
            put('A', 3, 'blue', '2026-11-10', fragments=1),
            put('B', 2, 'green', '2026-11-12', fragments=2)))
        self.assertEqual(result, {'schema_version': 1, 'head': 3,
                                  'emitted': True})
        # 换回使每桶计数不变的组合：B 回 blue/10 且 A 回 blue/10 的 single。
        result = apply_batch(store, batch(
            put('A', 4, 'blue', '2026-11-10', fragments=1),
            put('B', 3, 'blue', '2026-11-10', fragments=2)))
        self.assertEqual(result, {'schema_version': 1, 'head': 4,
                                  'emitted': True})

    def test_zero_net_swap_accepts_versions_without_entry(self):
        store = self.work / 'store.json'
        apply_batch(store, batch(
            put('A', 1, 'blue', '2026-11-10', fragments=1),
            put('B', 1, 'green', '2026-11-12', fragments=2)))
        before = snapshot(store)
        # 同一批里两条互换桶与档位：每个桶净变动为零。
        result = apply_batch(store, batch(
            put('A', 2, 'green', '2026-11-12', fragments=2),
            put('B', 2, 'blue', '2026-11-10', fragments=1)))
        self.assertEqual(result, {'schema_version': 1, 'head': 1,
                                  'emitted': False})
        self.assertEqual(snapshot(store)['buckets'], before['buckets'])
        # 版本确实被接纳：同版本同内容再次接入无动作。
        result = apply_batch(store, batch(
            put('A', 2, 'green', '2026-11-12', fragments=2)))
        self.assertEqual(result, {'schema_version': 1, 'head': 1,
                                  'emitted': False})

    def test_cursor_is_contiguous_across_many_batches(self):
        store = self.work / 'store.json'
        for index in range(5):
            result = apply_batch(store, batch(
                put('S', index + 1, f'c{index}', '2026-11-10', fragments=1)))
            self.assertEqual(result['head'], index + 1)
        page = changes(store, 0, 100)
        self.assertEqual([entry['cursor'] for entry in page['entries']],
                         [1, 2, 3, 4, 5])
        self.assertEqual(page['next'], 5)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.store = self.work / 'store.json'

    def tearDown(self):
        self.tmp.cleanup()

    def assert_rejected(self, data):
        with self.assertRaises(ValueError):
            apply_batch(self.store, data)
        self.assertFalse(self.store.exists())

    def test_batch_shape_rejections(self):
        self.assert_rejected({'records': []})
        self.assert_rejected({'records': [
            put(f's{index}', 1, 'blue', '2026-11-10') for index in range(8193)]})
        self.assert_rejected({'records': [], 'extra': 1})
        self.assert_rejected({'records': {'A': {}}})
        self.assert_rejected({'records': [put('A', 1, 'blue', '2026-11-10'),
                                          put('A', 2, 'blue', '2026-11-10')]})
        self.assert_rejected({'records': [{'submission_id': 'A', 'revision': 1}]})
        self.assert_rejected({'records': [
            {'submission_id': 'A', 'revision': 1, 'op': 'move'}]})

    def test_record_value_rejections(self):
        good = put('A', 1, 'blue', '2026-11-10')
        cases = [
            dict(revision=0), dict(revision=2147483648), dict(revision=True),
            dict(revision=1.0), dict(revision='1'),
            dict(course_id=''), dict(course_id='has space'),
            dict(day='2026-2-10'), dict(day='2026-02-30'), dict(day='20261110'),
            dict(day='2026-11-10T00:00'),
            dict(submission_id='bad id'),
        ]
        for override in cases:
            self.assert_rejected({'records': [{**good, **override}]})
        # cancel 不得带课程/日期/摘要；put 不得缺字段。
        self.assert_rejected({'records': [{
            **cancel('A', 1), 'course_id': 'blue'}]})
        for field in ('course_id', 'day', 'summary'):
            broken = dict(good)
            broken.pop(field)
            self.assert_rejected({'records': [broken]})
        # 布尔不能当作整数摘要字段。
        for override in (dict(bases=True), dict(fragments=True),
                         dict(unique_cuts=True), dict(recognition_hits=True)):
            value = summary()
            value.update(override)
            self.assert_rejected({'records': [{**good, 'summary': value}]})

    def test_summary_contract_rejections(self):
        for override in (dict(bases=0), dict(fragment_bases=1), dict(fragments=0),
                         dict(fragments=121), dict(unique_cuts=-1),
                         dict(recognition_hits=20000001)):
            value = summary()
            value.update(override)
            self.assert_rejected({'records': [{**put('A', 1, 'blue',
                                                     '2026-11-10'),
                                               'summary': value}]})
        for bad in ({'bases': 1}, dict(summary(), extra=1)):
            self.assert_rejected({'records': [{**put('A', 1, 'blue',
                                                     '2026-11-10'),
                                               'summary': bad}]})

    def test_stale_records_are_still_validated(self):
        apply_batch(self.store, batch(put('A', 2, 'blue', '2026-11-10')))
        before = snapshot(self.store)
        # 版本更低，但结构与值仍必须合法。
        with self.assertRaises(ValueError):
            apply_batch(self.store, batch(
                put('A', 1, 'blue', '2026-02-30')))
        with self.assertRaises(ValueError):
            apply_batch(self.store, batch(cancel('A', 0)))
        self.assertEqual(snapshot(self.store), before)
        # 合法但过时的记录被完整校验后接纳为无动作。
        result = apply_batch(self.store, batch(
            put('A', 1, 'green', '2026-12-01', fragments=1)))
        self.assertEqual(result, {'schema_version': 1, 'head': 1,
                                  'emitted': False})
        self.assertEqual(snapshot(self.store), before)

    def test_strict_json_boundaries_via_cli(self):
        cases = {
            'dup.json': ('{"records":[{"submission_id":"A","revision":1,'
                         '"op":"cancel"}],"records":[]}'),
            'nan.json': '{"records":NaN}',
            'bool.json': ('{"records":[{"submission_id":"A","revision":1,'
                          '"op":"put","course_id":"blue","day":"2026-11-10",'
                          '"summary":{"bases":true,"fragment_bases":1,'
                          '"fragments":1,"unique_cuts":0,"recognition_hits":0}}]}'),
        }
        for name, text in cases.items():
            path = self.work / name
            path.write_text(text, encoding='utf-8')
            result = run_cli('apply', '--store', str(self.store),
                             '--source', str(path))
            self.assertEqual(result.returncode, 2, (name, result.stdout))
            self.assertTrue(result.stderr)
            self.assertFalse(self.store.exists())
        big = self.work / 'big.json'
        big.write_bytes(b' ' * 8000001)
        result = run_cli('apply', '--store', str(self.store), '--source', str(big))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.store.exists())

    def test_cursor_and_limit_bounds(self):
        apply_batch(self.store, batch(put('A', 1, 'blue', '2026-11-10')))
        for after in (-1, 2):
            with self.assertRaises(ValueError):
                changes(self.store, after, 1)
        for limit in (0, 101):
            with self.assertRaises(ValueError):
                changes(self.store, 0, limit)
        with self.assertRaises(ValueError):
            changes(self.store, 0, True)
        # CLI 层同样退出 2。
        for args in (('--after', '-1', '--limit', '1'),
                     ('--after', '2', '--limit', '1'),
                     ('--after', '0', '--limit', '0'),
                     ('--after', '0', '--limit', '101')):
            result = run_cli('changes', '--store', str(self.store), *args)
            self.assertEqual(result.returncode, 2, args)
            self.assertEqual(result.stdout, '')

    def test_missing_store_is_empty_ledger(self):
        missing = self.work / 'nope' / 'store.json'
        self.assertEqual(snapshot(missing),
                         {'schema_version': 1, 'head': 0, 'buckets': []})
        self.assertEqual(changes(missing, 0, 5),
                         {'schema_version': 1, 'head': 0, 'next': 0,
                          'entries': []})

    def test_corrupt_store_rejected(self):
        self.store.write_text('{"ledger_format":1,"records":{},'
                              '"buckets":{"x":[1,2,3]},"entries":[]}',
                              encoding='utf-8')
        with self.assertRaises(ValueError):
            snapshot(self.store)
        with self.assertRaises(ValueError):
            apply_batch(self.store, batch(put('A', 1, 'blue', '2026-11-10')))
        result = run_cli('snapshot', '--store', str(self.store))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_v1_report_summary_feeds_ledger_without_opening_report(self):
        report_path = self.work / 'report.json'
        env = dict(os.environ)
        env['PYTHONPATH'] = str(SRC)
        result = subprocess.run(
            [sys.executable, '-P', '-m', 'sequence_digest.cli',
             '--source', str(V1_SOURCE), '--output', str(report_path)],
            cwd=str(ROOT), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(report_path.read_text(encoding='utf-8'))
        summary_only = dict(report['summary'])
        self.assertEqual(set(summary_only),
                         {'bases', 'fragment_bases', 'fragments',
                          'unique_cuts', 'recognition_hits'})
        # 只把五个整数原样带入台账；不读取片段、源字符串或切分规则。
        source = write_json(self.work / 'batch.json', {'records': [{
            'submission_id': 'ring', 'revision': 1, 'op': 'put',
            'course_id': 'blue', 'day': '2026-11-10',
            'summary': summary_only}]})
        store = self.work / 'store.json'
        result = run_cli('apply', '--store', str(store), '--source', str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = bucket_map(snapshot(store))
        self.assertEqual(rows[('blue', '2026-11-10')]['counts'],
                         {'single': 0, 'light': 1, 'heavy': 0})

    def test_existing_three_commands_unchanged(self):
        # 旧入口仍在注册表中，且 CLI 版本可正常执行。
        env = dict(os.environ)
        env['PYTHONPATH'] = str(SRC)
        for module, args in (
                ('sequence_digest.cli',
                 ['--source', str(V1_SOURCE),
                  '--output', str(self.work / 'report.json')]),
                ('sequence_digest.holdout_cli',
                 ['--source', str(ROOT / 'fixtures' / 'holdout' / 'acceptance.json'),
                  '--output', str(self.work / 'plan.json')]),
                ('sequence_digest.ledger_cli', ['--help']),
        ):
            result = subprocess.run(
                [sys.executable, '-P', '-m', module, *args],
                cwd=str(ROOT), env=env, text=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, (module, result.stderr))


if __name__ == '__main__':
    unittest.main()

"""阅卷盲测集编排测试：验收场景、独立枚举对照、非法输入与两组压测。"""
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
import unittest

from sequence_digest.holdout import HoldoutError, build_plan
from sequence_digest.io import load, publish

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
V1_SOURCE = ROOT / 'fixtures' / 'input.json'


def run_cli(*args, cwd=None):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(SRC)
    return subprocess.run(
        [sys.executable, '-P', '-m', 'sequence_digest.holdout_cli', *args],
        cwd=cwd or str(ROOT), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def summary(fragments=1, bases=10, unique_cuts=0, recognition_hits=0):
    return {
        'bases': bases,
        'fragment_bases': bases,
        'fragments': fragments,
        'unique_cuts': unique_cuts,
        'recognition_hits': recognition_hits,
    }


def submission(submission_id, fragments=1, **kwargs):
    return {'submission_id': submission_id, 'summary': summary(fragments, **kwargs)}


def batch(submissions, links, single, light, heavy):
    return {
        'submissions': [submission(sid, fragments) for sid, fragments in submissions],
        'links': links,
        'targets': {'single': single, 'light': light, 'heavy': heavy},
    }


def group_map(plan):
    return {group['group_id']: group for group in plan['groups']}


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8')
    return path


# --- 独立枚举对照：不复用 holdout 的任何判定逻辑 -------------------------

def oracle(groups, target):
    """暴力枚举全部 2**n 个组集合，返回 (解数, 组ID->状态, 规范划分)。"""
    vectors = [tuple(group['counts'][tier] for tier in ('single', 'light', 'heavy'))
               for group in groups]
    ids = [group['group_id'] for group in groups]
    feasible_masks = []
    for mask in range(1 << len(groups)):
        total = [0, 0, 0]
        for index, vector in enumerate(vectors):
            if mask >> index & 1:
                for dim in range(3):
                    total[dim] += vector[dim]
        if tuple(total) == tuple(target):
            feasible_masks.append(mask)
    count = len(feasible_masks)
    statuses = {}
    for index, group_id in enumerate(ids):
        included = sum(1 for mask in feasible_masks if mask >> index & 1)
        if count == 0:
            statuses[group_id] = None
        elif included == 0:
            statuses[group_id] = 'excluded'
        elif included == count:
            statuses[group_id] = 'required'
        else:
            statuses[group_id] = 'optional'
    if count == 0:
        canonical = None
    else:
        # 第一处不同优先包含：位串（组 0 为高位）按 1 优先取最大。
        def preference(mask):
            return tuple((mask >> index) & 1 for index in range(len(groups)))
        best = max(feasible_masks, key=preference)
        canonical = [ids[index] for index in range(len(groups))
                     if best >> index & 1]
    return count, statuses, canonical


class AcceptanceTests(unittest.TestCase):
    def test_v1_fixture_summary_is_accepted_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / 'report.json'
            env = dict(os.environ)
            env['PYTHONPATH'] = str(SRC)
            # 真正走一次旧 v1 入口，只从它的报告里取 summary。
            subprocess.run(
                [sys.executable, '-P', '-m', 'sequence_digest.cli',
                 '--source', str(V1_SOURCE), '--output', str(report_path)],
                cwd=str(ROOT), env=env, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            report = json.loads(report_path.read_text(encoding='utf-8'))
            summary_only = dict(report['summary'])
        # 批次只带摘要，不带片段、源字符串或切分规则。
        self.assertEqual(set(summary_only),
                         {'bases', 'fragment_bases', 'fragments',
                          'unique_cuts', 'recognition_hits'})
        data = {
            'submissions': [{'submission_id': 'ring', 'summary': summary_only}],
            'links': [],
            'targets': {'single': 0, 'light': 1, 'heavy': 0},
        }
        plan = build_plan(data)
        self.assertEqual(set(plan), {'schema_version', 'feasible_count',
                                     'blind_group_ids', 'groups'})
        self.assertEqual(plan['schema_version'], 1)
        self.assertEqual(plan['feasible_count'], 1)
        self.assertEqual(plan['blind_group_ids'], ['ring'])
        self.assertEqual(plan['groups'][0]['counts'],
                         {'single': 0, 'light': 1, 'heavy': 0})
        self.assertEqual(plan['groups'][0]['blind_status'], 'required')

    def test_acceptance_chain_scenarios(self):
        submissions = [('a', 1), ('d', 1), ('b', 2), ('e', 2),
                       ('c', 8), ('f', 8), ('g', 8)]
        chain_links = [['a', 'b'], ['b', 'c'], ['d', 'e'], ['e', 'f']]

        plan = build_plan(batch(submissions, chain_links, 2, 2, 2))
        self.assertEqual(plan['feasible_count'], 1)
        self.assertEqual(plan['blind_group_ids'], ['a', 'd'])
        statuses = {g['group_id']: g['blind_status'] for g in plan['groups']}
        self.assertEqual(statuses, {'a': 'required', 'd': 'required', 'g': 'excluded'})

        # 去掉 b—c：c 成为独立组，a/d 仍必选，c/g 二选一。
        plan = build_plan(batch(submissions, chain_links[:1] + chain_links[2:], 2, 2, 2))
        self.assertEqual(plan['feasible_count'], 2)
        self.assertEqual(plan['blind_group_ids'], ['a', 'c', 'd'])
        statuses = {g['group_id']: g['blind_status'] for g in plan['groups']}
        self.assertEqual(statuses, {'a': 'required', 'c': 'optional',
                                    'd': 'required', 'g': 'optional'})

        # 恢复 b—c，再加 c—d 把两条链并成一组；库存看似够但无解。
        merged = chain_links + [['c', 'd']]
        plan = build_plan(batch(submissions, merged, 1, 1, 1))
        self.assertEqual(plan['feasible_count'], 0)
        self.assertIsNone(plan['blind_group_ids'])
        self.assertTrue(all(g['blind_status'] is None for g in plan['groups']))
        self.assertEqual([g['group_id'] for g in plan['groups']], ['a', 'g'])
        self.assertEqual(plan['groups'][0]['member_ids'],
                         ['a', 'b', 'c', 'd', 'e', 'f'])

    def test_group_order_is_case_sensitive_ascii(self):
        # ASCII 中 'Z'(0x5A) < 'a'(0x61)；组 ID 与成员都按区分大小写的字典序。
        data = batch([('a1', 1), ('b2', 2), ('Z9', 8), ('z0', 8)],
                     [['a1', 'b2'], ['z0', 'Z9']], 0, 0, 2)
        plan = build_plan(data)
        self.assertEqual([g['group_id'] for g in plan['groups']], ['Z9', 'a1'])
        self.assertEqual(plan['groups'][0]['member_ids'], ['Z9', 'z0'])
        self.assertEqual(plan['groups'][1]['member_ids'], ['a1', 'b2'])

    def test_zero_targets_yield_exactly_one_empty_blind_set(self):
        data = batch([('a', 1), ('b', 2), ('c', 8)], [['a', 'b']], 0, 0, 0)
        plan = build_plan(data)
        self.assertEqual(plan['feasible_count'], 1)
        self.assertEqual(plan['blind_group_ids'], [])
        self.assertTrue(all(g['blind_status'] == 'excluded' for g in plan['groups']))

    def test_mixed_tiers_and_transitive_short_chain(self):
        # 一条短链 a—b—c 传递合并；组内三档混合，只有该组能凑出目标。
        data = batch([('a', 1), ('b', 2), ('c', 9), ('z', 8)],
                     [['c', 'b'], ['b', 'a']], 1, 1, 1)
        plan = build_plan(data)
        groups = group_map(plan)
        self.assertEqual(groups['a']['member_ids'], ['a', 'b', 'c'])
        self.assertEqual(groups['a']['counts'], {'single': 1, 'light': 1, 'heavy': 1})
        self.assertEqual(plan['feasible_count'], 1)
        self.assertEqual(plan['blind_group_ids'], ['a'])
        self.assertEqual(groups['a']['blind_status'], 'required')
        self.assertEqual(groups['z']['blind_status'], 'excluded')


class OracleCrossCheckTests(unittest.TestCase):
    """小批次随机对照：解数、逐组状态、规范划分都与暴力枚举一致。"""

    def test_random_small_batches_match_bruteforce(self):
        rng = random.Random(20261122)
        for trial in range(60):
            group_count = rng.randint(1, 9)
            groups = []
            submissions = []
            links = []
            for index in range(group_count):
                size = rng.randint(1, 3)
                member_ids = [f'g{index:02d}-{member:02d}' for member in range(size)]
                counts = {'single': 0, 'light': 0, 'heavy': 0}
                for member_id in member_ids:
                    fragments = rng.choice([1, 2, 7, 8, 66])
                    submissions.append((member_id, fragments))
                    tier = 'single' if fragments == 1 else (
                        'light' if fragments <= 7 else 'heavy')
                    counts[tier] += 1
                for left, right in zip(member_ids, member_ids[1:]):
                    links.append([right, left])  # 故意反向，验证无向
                groups.append({'group_id': member_ids[0],
                               'member_ids': member_ids,
                               'counts': counts})
            target = tuple(rng.randint(0, 3) for _ in range(3))
            data = batch(submissions, links, *target)
            for item in data['submissions']:
                item['summary']['bases'] = 100
                item['summary']['fragment_bases'] = 100
            plan = build_plan(data)

            expected_count, expected_status, expected_plan = oracle(groups, target)
            self.assertEqual(plan['feasible_count'], expected_count, (trial, target))
            actual = {g['group_id']: g['blind_status'] for g in plan['groups']}
            self.assertEqual(actual, expected_status, (trial, target))
            self.assertEqual(plan['blind_group_ids'], expected_plan, (trial, target))

    def test_identical_counts_are_not_merged(self):
        # 两个组计数完全相同，方案数按身份计为 2，而不是 1。
        data = batch([('a', 1), ('b', 1)], [], 1, 0, 0)
        plan = build_plan(data)
        self.assertEqual(plan['feasible_count'], 2)
        self.assertEqual([g['blind_status'] for g in plan['groups']],
                         ['optional', 'optional'])
        self.assertEqual(plan['blind_group_ids'], ['a'])


class ValidationTests(unittest.TestCase):
    def assert_rejected(self, data):
        with self.assertRaises((HoldoutError, ValueError)):
            build_plan(data)

    def test_structural_rejections(self):
        good = batch([('a', 1)], [], 1, 0, 0)
        self.assert_rejected({k: v for k, v in good.items() if k != 'links'})
        extra = dict(good)
        extra['extra'] = 1
        self.assert_rejected(extra)
        self.assert_rejected({**good, 'submissions': []})
        self.assert_rejected({**good, 'submissions': [{'submission_id': 'a'}]})
        self.assert_rejected({**good, 'submissions': [
            {'submission_id': 'a', 'summary': summary(), 'x': 1}]})
        self.assert_rejected({**good, 'links': [['a']]})
        self.assert_rejected({**good, 'links': [['a', 'missing']]})
        self.assert_rejected({**good, 'links': [['a', 'a', 'a']]})
        self.assert_rejected({**good, 'links': 'nope'})
        self.assert_rejected({**good, 'targets': {'single': 0, 'light': 0}})
        self.assert_rejected({**good, 'targets': {'single': 0, 'light': 0,
                                                  'heavy': 0, 'x': 0}})

    def test_duplicate_submission_ids_rejected(self):
        data = batch([('a', 1), ('a', 2)], [], 1, 0, 0)
        self.assert_rejected(data)

    def test_summary_contract_rejections(self):
        cases = [
            dict(bases=0),
            dict(bases=True),
            dict(fragment_bases=9),
            dict(fragments=0),
            dict(fragments=11),
            dict(fragments=1.5),
            dict(unique_cuts=-1),
            dict(unique_cuts=11),
            dict(recognition_hits=-1),
            dict(recognition_hits=20000001),
        ]
        for override in cases:
            value = summary()
            value.update(override)
            data = {'submissions': [{'submission_id': 'a', 'summary': value}],
                    'links': [], 'targets': {'single': 0, 'light': 0, 'heavy': 0}}
            self.assert_rejected(data)
        # recognition_hits 不小于 unique_cuts。
        value = summary(unique_cuts=3, recognition_hits=2)
        self.assert_rejected({'submissions': [
            {'submission_id': 'a', 'summary': value}],
            'links': [], 'targets': {'single': 0, 'light': 0, 'heavy': 0}})
        # 摘要缺字段或多余字段。
        for bad in ({'bases': 1}, dict(summary(), extra=1)):
            self.assert_rejected({'submissions': [
                {'submission_id': 'a', 'summary': bad}],
                'links': [], 'targets': {'single': 0, 'light': 0, 'heavy': 0}})

    def test_targets_bounds_and_booleans(self):
        for target in (-1, 8193, True):
            data = batch([('a', 1)], [], 0, 0, 0)
            data['targets']['heavy'] = target
            self.assert_rejected(data)

    def test_more_than_32_groups_rejected(self):
        submissions = [(f's{i:02d}', 1) for i in range(33)]
        self.assert_rejected(batch(submissions, [], 1, 0, 0))

    def test_target_above_inventory_is_feasible_zero_not_error(self):
        plan = build_plan(batch([('a', 1)], [], 2, 0, 0))
        self.assertEqual(plan['feasible_count'], 0)
        self.assertIsNone(plan['blind_group_ids'])
        self.assertEqual(plan['groups'][0]['blind_status'], None)

    def test_duplicate_and_reversed_edges_are_idempotent(self):
        base = [('a', 1), ('b', 2), ('c', 8)]
        links = [['a', 'b'], ['b', 'c']]
        first = build_plan(batch(base, links, 1, 1, 1))
        second = build_plan(batch(base, links + [['b', 'a'], ['c', 'b'], ['a', 'a']],
                                  1, 1, 1))
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))


class InputBoundaryTests(unittest.TestCase):
    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'dup.json'
            path.write_text(
                '{"submissions":[],"submissions":[],"links":[],'
                '"targets":{"single":0,"light":0,"heavy":0}}', encoding='utf-8')
            with self.assertRaises(ValueError):
                load(path)

    def test_nonfinite_and_oversized_input_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'nan.json'
            path.write_text('{"submissions":NaN,"links":[],'
                            '"targets":{"single":0,"light":0,"heavy":0}}',
                            encoding='utf-8')
            with self.assertRaises(ValueError):
                load(path)
            big = Path(tmp) / 'big.json'
            big.write_bytes(b' ' * 8000001)
            with self.assertRaises(ValueError):
                load(big)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_writes_plan_and_refuses_existing_target_including_symlink(self):
        source = write_json(self.work / 'batch.json',
                            batch([('a', 1), ('b', 2)], [['a', 'b']], 1, 1, 0))
        output = self.work / 'plan.json'
        result = run_cli('--source', str(source), '--output', str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['feasible_count'], 1)
        original = output.read_bytes()
        payload = json.loads(original)
        self.assertEqual(payload['schema_version'], 1)
        self.assertEqual(payload['blind_group_ids'], ['a'])

        result = run_cli('--source', str(source), '--output', str(output))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(output.read_bytes(), original)

        link = self.work / 'link.json'
        link.symlink_to(output)
        result = run_cli('--source', str(source), '--output', str(link))
        self.assertEqual(result.returncode, 2)
        self.assertTrue(link.is_symlink())
        self.assertEqual(output.read_bytes(), original)

    def test_cli_invalid_input_exits_2_and_writes_nothing(self):
        source = write_json(self.work / 'bad.json',
                            batch([('a', 1), ('a', 1)], [], 1, 0, 0))
        output = self.work / 'never.json'
        result = run_cli('--source', str(source), '--output', str(output))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())

    def test_output_bytes_are_order_independent(self):
        submissions = [('a', 1), ('b', 2), ('c', 8), ('d', 1), ('e', 2)]
        links = [['a', 'b'], ['b', 'c'], ['d', 'e']]
        first = build_plan(batch(submissions, links, 2, 2, 1))
        shuffled_submissions = list(submissions)
        shuffled_links = [list(reversed(link)) for link in links]
        random.Random(7).shuffle(shuffled_submissions)
        random.Random(9).shuffle(shuffled_links)
        second = build_plan(batch(shuffled_submissions,
                                  shuffled_links + [['e', 'd']], 2, 2, 1))
        out_a = self.work / 'a.json'
        out_b = self.work / 'b.json'
        publish(out_a, first)
        publish(out_b, second)
        self.assertEqual(out_a.read_bytes(), out_b.read_bytes())


class StressTests(unittest.TestCase):
    """单批 20 秒预算；从 CLI 读取现成批次到计划写完计时。"""

    BUDGET_SECONDS = 20.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_and_time(self, source, output):
        start = time.monotonic()
        result = run_cli('--source', str(source), '--output', str(output))
        elapsed = time.monotonic() - start
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, self.BUDGET_SECONDS,
                        f'batch took {elapsed:.3f}s')
        return elapsed, json.loads(output.read_text(encoding='utf-8'))

    def _write(self, name, data):
        import json as _json
        path = self.work / name
        path.write_text(_json.dumps(data), encoding='utf-8')
        return path

    def test_32_identical_three_tier_groups(self):
        submissions = []
        links = []
        for block in range(32):
            ids = [f'g{block:02d}-h', f'g{block:02d}-l', f'g{block:02d}-s']
            submissions.extend([
                {'submission_id': ids[0], 'summary': summary(8)},
                {'submission_id': ids[1], 'summary': summary(2)},
                {'submission_id': ids[2], 'summary': summary(1)},
            ])
            links.extend([['g%02d-h' % block, 'g%02d-l' % block],
                          ['g%02d-l' % block, 'g%02d-s' % block]])
        data = {'submissions': submissions, 'links': links,
                'targets': {'single': 16, 'light': 16, 'heavy': 16}}
        source = self._write('block1.json', data)
        _elapsed, plan = self._run_and_time(source, self.work / 'plan1.json')
        self.assertEqual(plan['feasible_count'], 601080390)
        self.assertTrue(all(g['blind_status'] == 'optional' for g in plan['groups']))
        self.assertEqual(plan['blind_group_ids'],
                         [f'g{block:02d}-h' for block in range(16)])

    def test_three_geometric_series_of_chained_groups(self):
        bases = ((2, 1, 1), (1, 2, 1), (1, 1, 2))
        submissions = []
        links = []
        for series, (singles, lights, heavies) in enumerate(bases):
            for step in range(9):
                multiplier = 1 << step
                members = []
                for tier, fragments, amount in (
                        ('s', 1, singles), ('l', 2, lights), ('h', 8, heavies)):
                    for copy in range(amount * multiplier):
                        sid = f's{series}g{step}{tier}{copy:04d}'
                        members.append(sid)
                        submissions.append({'submission_id': sid,
                                            'summary': summary(fragments)})
                members.sort()
                links.extend([[left, right]
                              for left, right in zip(members, members[1:])])
        self.assertEqual(len(submissions), 6132)
        data = {'submissions': submissions, 'links': links,
                'targets': {'single': 1020, 'light': 1020, 'heavy': 1020}}
        source = self._write('block2.json', data)
        _elapsed, plan = self._run_and_time(source, self.work / 'plan2.json')
        self.assertEqual(plan['feasible_count'], 1)
        statuses = {g['group_id']: g['blind_status'] for g in plan['groups']}
        expected_blind = []
        for series in range(3):
            for step in range(8):
                expected_blind.append(f's{series}g{step}h0000')
        self.assertEqual(sorted(plan['blind_group_ids']), sorted(expected_blind))
        for series in range(3):
            self.assertEqual(statuses[f's{series}g8h0000'], 'excluded')
            for step in range(8):
                self.assertEqual(statuses[f's{series}g{step}h0000'], 'required')


if __name__ == '__main__':
    unittest.main()

"""复核产能排班测试：验收场景、独立枚举对照、容量批次与输入/输出边界。"""
import json
import os
from pathlib import Path
import random
import resource
import subprocess
import sys
import tempfile
import unittest

from sequence_digest.io import load
from sequence_digest.staff import StaffError, build_plan

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
FIXTURES = ROOT / 'fixtures' / 'staff'
SNAPSHOT = FIXTURES / 'snapshot.json'
ROSTER = FIXTURES / 'roster.json'
LEDGER_INITIAL = FIXTURES / 'ledger-initial.json'
LEDGER_CORRECTION = FIXTURES / 'ledger-correction.json'

BLUE = ('Blue', '2026-11-10')
RED = ('Red', '2026-11-11')


def run_module(module, *args, cwd=None, env_extra=None, limit_address_space=None):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(SRC)
    if env_extra:
        env.update(env_extra)

    def preexec():
        if limit_address_space is not None:
            resource.setrlimit(resource.RLIMIT_AS,
                               (limit_address_space, limit_address_space))

    return subprocess.run(
        [sys.executable, '-P', '-m', module, *args],
        cwd=cwd or str(ROOT), env=env, text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        preexec_fn=preexec if limit_address_space is not None else None,
    )


def run_cli(*args, **kwargs):
    return run_module('sequence_digest.staff_cli', *args, **kwargs)


def run_ledger(*args):
    return run_module('sequence_digest.ledger_cli', *args)


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8')
    return path


def heavy_snapshot(blue=6, red=6, scale=1):
    return {
        'schema_version': 1, 'head': 2,
        'buckets': [
            {'course_id': 'Blue', 'day': '2026-11-10',
             'counts': {'single': 0, 'light': 0, 'heavy': blue // 6 * scale},
             'points': blue * scale},
            {'course_id': 'Red', 'day': '2026-11-11',
             'counts': {'single': 0, 'light': 0, 'heavy': red // 6 * scale},
             'points': red * scale},
        ],
    }


def three_shifts(capacity=6):
    return [
        {'shift_id': 'F', 'day': '2026-11-10',
         'courses': ['Blue', 'Red'], 'capacity': capacity},
        {'shift_id': 'B0', 'day': '2026-11-10',
         'courses': ['Blue'], 'capacity': capacity},
        {'shift_id': 'B1', 'day': '2026-11-10',
         'courses': ['Blue'], 'capacity': capacity},
    ]


def roster(shifts=None, bookings=None, start='2026-11-10', end='2026-11-11'):
    return {'start_day': start, 'end_day': end,
            'shifts': shifts if shifts is not None else three_shifts(),
            'bookings': bookings or []}


def assignment_map(plan):
    return {(row['shift_id'], row['course_id'], row['day']):
            (row['points'], row['locked_points'])
            for row in plan['assignments']}


def bucket_map(plan):
    return {(row['course_id'], row['day']): row for row in plan['buckets']}


class AcceptanceScenarioTests(unittest.TestCase):
    """验收第 1 条：早/更正两版快照与三种更正情形。"""

    def test_ledger_exports_two_snapshot_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / 'ledger.json'
            self.assertEqual(run_ledger('apply', '--store', str(store),
                                        '--source', str(LEDGER_INITIAL)).returncode, 0)
            early = json.loads(run_ledger('snapshot', '--store', str(store)).stdout)
            self.assertEqual(early['buckets'],
                             [{'counts': {'single': 0, 'light': 1, 'heavy': 1},
                               'course_id': 'Blue', 'day': '2026-11-10',
                               'points': 9}])
            self.assertEqual(run_ledger('apply', '--store', str(store),
                                        '--source', str(LEDGER_CORRECTION)).returncode, 0)
            corrected = json.loads(run_ledger('snapshot', '--store', str(store)).stdout)
            self.assertEqual(corrected['head'], 2)
            self.assertEqual(bucket_map(corrected) if False else
                             {(b['course_id'], b['day']): b['points']
                              for b in corrected['buckets']},
                             {BLUE: 6, RED: 6})

    def test_corrected_snapshot_rebalances_to_cover_nine(self):
        plan = build_plan(load(SNAPSHOT), load(ROSTER))
        self.assertEqual(plan['head'], 2)
        rows = assignment_map(plan)
        self.assertEqual(rows[('F', 'Blue', '2026-11-10')], (3, 3))
        self.assertEqual(rows[('F', 'Red', '2026-11-11')], (3, 0))
        self.assertEqual(rows[('B1', 'Blue', '2026-11-10')], (3, 0))
        self.assertNotIn(('B0', 'Blue', '2026-11-10'), rows)
        self.assertEqual(plan['covered_points'], 9)
        self.assertEqual(plan['retained_provisional_points'], 3)
        self.assertEqual(bucket_map(plan)[RED]['shortage_points'], 3)

    def test_unlocking_everything_lets_red_take_six(self):
        draft = roster(bookings=[
            {'shift_id': 'F', 'course_id': 'Blue', 'day': '2026-11-10',
             'points': 6, 'locked_points': 0},
            {'shift_id': 'B1', 'course_id': 'Blue', 'day': '2026-11-10',
             'points': 3, 'locked_points': 0},
        ])
        plan = build_plan(load(SNAPSHOT), draft)
        rows = assignment_map(plan)
        self.assertEqual(rows[('F', 'Red', '2026-11-11')], (6, 0))
        self.assertEqual(plan['covered_points'], 12)
        self.assertEqual(plan['retained_provisional_points'], 3)
        self.assertEqual(rows[('B1', 'Blue', '2026-11-10')], (3, 0))

    def test_withdrawn_blue_demand_keeps_confirmation_as_surplus(self):
        snapshot = {'schema_version': 1, 'head': 3, 'buckets': [
            {'course_id': 'Red', 'day': '2026-11-11',
             'counts': {'single': 0, 'light': 0, 'heavy': 1}, 'points': 6}]}
        draft = roster(bookings=[
            {'shift_id': 'F', 'course_id': 'Blue', 'day': '2026-11-10',
             'points': 6, 'locked_points': 3}])
        plan = build_plan(snapshot, draft)
        rows = assignment_map(plan)
        self.assertEqual(rows[('F', 'Blue', '2026-11-10')], (3, 3))
        self.assertEqual(rows[('F', 'Red', '2026-11-11')], (3, 0))
        blue = bucket_map(plan)[BLUE]
        self.assertEqual((blue['demand_points'], blue['covered_points'],
                          blue['shortage_points'], blue['locked_surplus_points']),
                         (0, 0, 0, 3))
        self.assertEqual(bucket_map(plan)[RED]['shortage_points'], 3)
        self.assertEqual(plan['covered_points'], 3)

    def test_head_is_bound_to_the_supplied_snapshot(self):
        snapshot = load(SNAPSHOT)
        snapshot['head'] = 7
        plan = build_plan(snapshot, load(ROSTER))
        self.assertEqual(plan['head'], 7)


class CapacityBatchTests(unittest.TestCase):
    """验收第 2 条：128 份互不共享班次/课程的千倍副本，受限进程内一次排完。"""

    COPIES = 128
    SCALE = 1000
    ADDRESS_SPACE = 256 * 1024 * 1024

    def _build_batch(self):
        buckets, shifts, bookings = [], [], []
        for index in range(self.COPIES):
            prefix = f'c{index:03d}_'
            for course, day in (('Blue', '2026-11-10'), ('Red', '2026-11-11')):
                buckets.append({
                    'course_id': prefix + course, 'day': day,
                    'counts': {'single': 0, 'light': 0,
                               'heavy': 6 // 6 * self.SCALE},
                    'points': 6 * self.SCALE})
            for shift_id, courses in (('F', ['Blue', 'Red']),
                                      ('B0', ['Blue']), ('B1', ['Blue'])):
                shifts.append({
                    'shift_id': prefix + shift_id, 'day': '2026-11-10',
                    'courses': [prefix + course for course in courses],
                    'capacity': 6 * self.SCALE})
            bookings.append({
                'shift_id': prefix + 'F', 'course_id': prefix + 'Blue',
                'day': '2026-11-10', 'points': 6 * self.SCALE,
                'locked_points': 3 * self.SCALE})
            bookings.append({
                'shift_id': prefix + 'B1', 'course_id': prefix + 'Blue',
                'day': '2026-11-10', 'points': 3 * self.SCALE,
                'locked_points': 0})
        snapshot = {'schema_version': 1, 'head': 2, 'buckets': buckets}
        draft = {'start_day': '2026-11-10', 'end_day': '2026-11-11',
                 'shifts': shifts, 'bookings': bookings}
        return snapshot, draft

    def test_batch_matches_single_copy_under_address_limit(self):
        snapshot, draft = self._build_batch()
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            snapshot_path = write_json(work / 'snapshot.json', snapshot)
            roster_path = write_json(work / 'roster.json', draft)
            output = work / 'plan.json'
            result = run_cli('--snapshot', str(snapshot_path),
                             '--roster', str(roster_path),
                             '--output', str(output),
                             limit_address_space=self.ADDRESS_SPACE)
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(output.read_text(encoding='utf-8'))

        self.assertEqual(plan['covered_points'], 1152000)
        self.assertEqual(plan['retained_provisional_points'], 384000)

        # 逐桶、逐分配与第 1 条单副本结果一致（仅编号带前缀、点数乘 1000）。
        single = build_plan(load(SNAPSHOT), load(ROSTER))
        single_rows = assignment_map(single)
        single_buckets = bucket_map(single)
        plan_rows = assignment_map(plan)
        plan_buckets = bucket_map(plan)
        for index in range(self.COPIES):
            prefix = f'c{index:03d}_'
            self.assertEqual(len(plan_rows), 3 * self.COPIES)
            for (shift_id, course, day), value in single_rows.items():
                scaled = (value[0] * self.SCALE, value[1] * self.SCALE)
                self.assertEqual(plan_rows[(prefix + shift_id, prefix + course,
                                            day)], scaled)
            for (course, day), value in single_buckets.items():
                scaled = dict(value)
                for field in ('demand_points', 'covered_points',
                              'shortage_points', 'locked_surplus_points'):
                    scaled[field] *= self.SCALE
                scaled['course_id'] = prefix + course
                self.assertEqual(plan_buckets[(prefix + course, day)], scaled)
        self.assertEqual(len(plan_buckets), 2 * self.COPIES)


# --- 独立穷举对照：不复用 staff 的任何判定逻辑 -----------------------------

def oracle(snapshot, draft):
    """枚举全部可行整数点分配，返回 (最大覆盖, 该覆盖下的最大保留)。

    只在很小的整数点对上使用；确认与资格规则在这里独立重写一遍。
    """
    start, end = draft['start_day'], draft['end_day']
    shifts = {s['shift_id']: s for s in draft['shifts']}
    demand = {(b['course_id'], b['day']): b['points']
              for b in snapshot['buckets']
              if start <= b['day'] <= end and b['points'] > 0}
    buckets = sorted(demand)

    # 候选边：班次容量扣掉确认后的残量 → 桶剩余需求。
    locked_on_shift = {}
    locked_on_bucket = {}
    for booking in draft['bookings']:
        if booking['locked_points']:
            locked_on_shift[booking['shift_id']] = \
                locked_on_shift.get(booking['shift_id'], 0) + booking['locked_points']
            key = (booking['course_id'], booking['day'])
            locked_on_bucket[key] = \
                locked_on_bucket.get(key, 0) + booking['locked_points']
    residual = {shift_id: shifts[shift_id]['capacity'] - locked_on_shift.get(shift_id, 0)
                for shift_id in shifts}
    original = {}
    for booking in draft['bookings']:
        bound = booking['points'] - booking['locked_points']
        if bound > 0:
            key = (booking['shift_id'], booking['course_id'], booking['day'])
            original[key] = max(original.get(key, 0), bound)

    edges = []
    for shift_id, shift in shifts.items():
        for key in buckets:
            if key[0] in shift['courses'] and key[1] >= shift['day'] \
                    and residual[shift_id] > 0 and demand[key] > 0:
                cap = min(residual[shift_id], demand[key])
                edges.append((shift_id, key, cap))

    best_covered = -1
    best_retained = -1
    # 每条边的取值枚举 0..cap；边数很少时可行。取上下界剪枝不需要。
    values = [0] * len(edges)

    def evaluate():
        used_shift = {}
        used_bucket = {}
        retained = 0
        for (shift_id, key, _), amount in zip(edges, values):
            used_shift[shift_id] = used_shift.get(shift_id, 0) + amount
            used_bucket[key] = used_bucket.get(key, 0) + amount
            bound = original.get((shift_id, key[0], key[1]), 0)
            retained += min(amount, bound)
        for shift_id, amount in used_shift.items():
            if amount > residual[shift_id]:
                return None
        for key, amount in used_bucket.items():
            # 可改点不得让该桶超出剩余需求（确认已抵扣后仍不能造成超额）。
            residual_need = max(0, demand[key] - locked_on_bucket.get(key, 0))
            if amount > residual_need:
                return None
        covered = sum(min(demand[key], locked_on_bucket.get(key, 0)
                          + used_bucket.get(key, 0)) for key in buckets)
        return covered, retained

    def recurse(index):
        nonlocal best_covered, best_retained
        if index == len(edges):
            score = evaluate()
            if score is not None:
                covered, retained = score
                if covered > best_covered or (
                        covered == best_covered and retained > best_retained):
                    best_covered, best_retained = covered, retained
            return
        cap = edges[index][2]
        for amount in range(cap + 1):
            values[index] = amount
            recurse(index + 1)
        values[index] = 0

    recurse(0)
    return best_covered, best_retained


class OracleCrossCheckTests(unittest.TestCase):
    """验收第 3 条：小计划随机对照与两个目标值。"""

    def test_plan_targets_match_bruteforce(self):
        rng = random.Random(20261122)
        checked = 0
        for _ in range(40):
            courses = ['a', 'b']
            days = ['2026-11-10', '2026-11-11']
            buckets = []
            for course in courses:
                for day in days:
                    points = rng.randint(0, 4)
                    if points == 0:
                        continue
                    buckets.append({
                        'course_id': course, 'day': day,
                        'counts': {'single': points, 'light': 0, 'heavy': 0},
                        'points': points})
            if not buckets:
                continue
            snapshot = {'schema_version': 1, 'head': 1, 'buckets': buckets}
            shifts = []
            for index in range(rng.randint(1, 3)):
                courses_for = rng.sample(courses, rng.randint(1, len(courses)))
                shifts.append({
                    'shift_id': f's{index}', 'day': rng.choice(days),
                    'courses': courses_for, 'capacity': rng.randint(0, 5)})
            bookings = []
            for index in range(rng.randint(0, 2)):
                shift = shifts[index % len(shifts)]
                course = rng.choice(shift['courses'])
                limit = shift['capacity']
                total = rng.randint(1, 4)
                locked = rng.randint(0, total)
                bookings.append({
                    'shift_id': shift['shift_id'], 'course_id': course,
                    'day': '2026-11-11', 'points': total,
                    'locked_points': locked})
            draft = {'start_day': '2026-11-10', 'end_day': '2026-11-11',
                     'shifts': shifts, 'bookings': bookings}
            try:
                plan = build_plan(snapshot, draft)
            except StaffError:
                continue
            expected = oracle(snapshot, draft)
            self.assertEqual(
                (plan['covered_points'], plan['retained_provisional_points']),
                expected, (snapshot, draft))
            checked += 1
        self.assertGreater(checked, 20)

    def test_flexible_shift_must_serve_the_constrained_course(self):
        # only F can serve Red; if F goes to Blue, Red is uncovered.
        snapshot = {'schema_version': 1, 'head': 1, 'buckets': [
            {'course_id': 'Blue', 'day': '2026-11-11',
             'counts': {'single': 0, 'light': 0, 'heavy': 1}, 'points': 6},
            {'course_id': 'Red', 'day': '2026-11-11',
             'counts': {'single': 0, 'light': 0, 'heavy': 1}, 'points': 6}]}
        shifts = [
            {'shift_id': 'F', 'day': '2026-11-10',
             'courses': ['Blue', 'Red'], 'capacity': 6},
            {'shift_id': 'B', 'day': '2026-11-10',
             'courses': ['Blue'], 'capacity': 6}]
        draft = {'start_day': '2026-11-10', 'end_day': '2026-11-11',
                 'shifts': shifts, 'bookings': [
                     {'shift_id': 'F', 'course_id': 'Blue',
                      'day': '2026-11-11', 'points': 6, 'locked_points': 0}]}
        plan = build_plan(snapshot, draft)
        rows = assignment_map(plan)
        self.assertEqual(plan['covered_points'], 12)
        self.assertEqual(rows[('F', 'Red', '2026-11-11')], (6, 0))
        self.assertEqual(rows[('B', 'Blue', '2026-11-11')], (6, 0))
        self.assertEqual(plan['retained_provisional_points'], 0)

    def test_same_coverage_but_different_retention(self):
        # both shifts can cover Blue; only the original pair gives retention.
        snapshot = {'schema_version': 1, 'head': 1, 'buckets': [
            {'course_id': 'Blue', 'day': '2026-11-11',
             'counts': {'single': 0, 'light': 0, 'heavy': 1}, 'points': 6}]}
        shifts = [
            {'shift_id': 'A', 'day': '2026-11-10',
             'courses': ['Blue'], 'capacity': 6},
            {'shift_id': 'B', 'day': '2026-11-10',
             'courses': ['Blue'], 'capacity': 6}]
        draft = {'start_day': '2026-11-10', 'end_day': '2026-11-11',
                 'shifts': shifts, 'bookings': [
                     {'shift_id': 'B', 'course_id': 'Blue',
                      'day': '2026-11-11', 'points': 4, 'locked_points': 0}]}
        plan = build_plan(snapshot, draft)
        self.assertEqual(plan['covered_points'], 6)
        self.assertEqual(plan['retained_provisional_points'], 4)
        rows = assignment_map(plan)
        self.assertEqual(rows[('B', 'Blue', '2026-11-11')], (4, 0))


class BoundaryTests(unittest.TestCase):
    """验收第 3 条余项：日期、容量、资格与输入重排。"""

    def test_shift_after_settlement_day_is_ineligible(self):
        snapshot = {'schema_version': 1, 'head': 1, 'buckets': [
            {'course_id': 'Blue', 'day': '2026-11-10',
             'counts': {'single': 6, 'light': 0, 'heavy': 0}, 'points': 6}]}
        draft = {'start_day': '2026-11-10', 'end_day': '2026-11-12', 'shifts': [
            {'shift_id': 'late', 'day': '2026-11-11',
             'courses': ['Blue'], 'capacity': 6}], 'bookings': []}
        plan = build_plan(snapshot, draft)
        self.assertEqual(plan['covered_points'], 0)
        self.assertEqual(plan['assignments'], [])

    def test_confirmed_over_capacity_is_rejected(self):
        snapshot = load(SNAPSHOT)
        draft = roster(shifts=[{'shift_id': 'F', 'day': '2026-11-10',
                                'courses': ['Blue', 'Red'], 'capacity': 2}],
                       bookings=[{'shift_id': 'F', 'course_id': 'Blue',
                                  'day': '2026-11-10', 'points': 2,
                                  'locked_points': 3}])
        with self.assertRaises(StaffError):
            build_plan(snapshot, draft)

    def test_withdrawn_shift_invalidates_only_confirmed_bookings(self):
        snapshot = load(SNAPSHOT)
        # unconfirmed booking on a withdrawn shift is simply dropped, so the
        # freed capacity covers everything (12).
        plan = build_plan(snapshot, roster(bookings=[
            {'shift_id': 'gone', 'course_id': 'Blue', 'day': '2026-11-10',
             'points': 6, 'locked_points': 0}]))
        self.assertEqual(plan['covered_points'], 12)
        self.assertEqual(plan['retained_provisional_points'], 0)
        # a positive confirmation on that shift is a hard error.
        with self.assertRaises(StaffError):
            build_plan(snapshot, roster(bookings=[
                {'shift_id': 'gone', 'course_id': 'Blue',
                 'day': '2026-11-10', 'points': 6, 'locked_points': 1}]))

    def test_ineligible_confirmation_is_rejected_but_unconfirmed_is_not(self):
        snapshot = load(SNAPSHOT)
        with self.assertRaises(StaffError):
            build_plan(snapshot, roster(bookings=[
                {'shift_id': 'B0', 'course_id': 'Red',
                 'day': '2026-11-11', 'points': 6, 'locked_points': 1}]))
        plan = build_plan(snapshot, roster(bookings=[
            {'shift_id': 'B0', 'course_id': 'Red',
             'day': '2026-11-11', 'points': 6, 'locked_points': 0}]))
        self.assertEqual(plan['covered_points'], 12)

    def test_input_order_does_not_change_output_bytes(self):
        snapshot = load(SNAPSHOT)
        draft = load(ROSTER)
        baseline = json.dumps(build_plan(snapshot, draft), sort_keys=True,
                              separators=(',', ':'))
        rng = random.Random(7)
        for _ in range(5):
            shuffled_snapshot = dict(snapshot)
            shuffled_snapshot['buckets'] = list(snapshot['buckets'])
            rng.shuffle(shuffled_snapshot['buckets'])
            shuffled_draft = dict(draft)
            shuffled_draft['shifts'] = list(draft['shifts'])
            rng.shuffle(shuffled_draft['shifts'])
            shuffled_draft['bookings'] = list(draft['bookings'])
            rng.shuffle(shuffled_draft['bookings'])
            again = json.dumps(build_plan(shuffled_snapshot, shuffled_draft),
                               sort_keys=True, separators=(',', ':'))
            self.assertEqual(again, baseline)

    def test_demand_without_any_eligible_shift_is_a_valid_plan(self):
        snapshot = {'schema_version': 1, 'head': 1, 'buckets': [
            {'course_id': 'Blue', 'day': '2026-11-11',
             'counts': {'single': 6, 'light': 0, 'heavy': 0}, 'points': 6}]}
        draft = {'start_day': '2026-11-10', 'end_day': '2026-11-11',
                 'shifts': [], 'bookings': []}
        plan = build_plan(snapshot, draft)
        self.assertEqual(plan['covered_points'], 0)
        self.assertEqual(bucket_map(plan)[('Blue', '2026-11-11')]['shortage_points'], 6)

    def test_duplicate_booking_pair_is_rejected(self):
        snapshot = load(SNAPSHOT)
        draft = roster(bookings=[
            {'shift_id': 'F', 'course_id': 'Blue', 'day': '2026-11-10',
             'points': 6, 'locked_points': 0},
            {'shift_id': 'F', 'course_id': 'Blue', 'day': '2026-11-10',
             'points': 3, 'locked_points': 0}])
        with self.assertRaises(StaffError):
            build_plan(snapshot, draft)

    def test_structural_rejections(self):
        snapshot = load(SNAPSHOT)
        bad_roster = roster()
        bad_roster['extra'] = 1
        with self.assertRaises(StaffError):
            build_plan(snapshot, bad_roster)
        with self.assertRaises(StaffError):
            build_plan({'schema_version': 1, 'head': 0, 'buckets': [
                {'course_id': 'Blue', 'day': '2026-11-10',
                 'counts': {'single': 1, 'light': 0, 'heavy': 0},
                 'points': 2}]}, roster())
        # boolean is not an integer
        with self.assertRaises(StaffError):
            build_plan(snapshot, roster(shifts=[
                {'shift_id': 'F', 'day': '2026-11-10', 'courses': ['Blue'],
                 'capacity': True}]))
        # plan period longer than 31 days
        with self.assertRaises(StaffError):
            build_plan(snapshot, roster(start='2026-01-01', end='2026-02-05'))

    def test_too_many_eligible_pairs_are_rejected(self):
        # 100 shifts × 121 eligible courses = 12100 distinct pairs > 12000.
        courses = [f'C{index:03d}' for index in range(121)]
        buckets = [{'course_id': course, 'day': '2026-11-11',
                    'counts': {'single': 1, 'light': 0, 'heavy': 0},
                    'points': 1} for course in courses]
        shifts = [{'shift_id': f's{index:05d}', 'day': '2026-11-10',
                   'courses': courses, 'capacity': 1}
                  for index in range(100)]
        with self.assertRaises(StaffError):
            build_plan({'schema_version': 1, 'head': 0, 'buckets': buckets},
                       {'start_day': '2026-11-10', 'end_day': '2026-11-11',
                        'shifts': shifts, 'bookings': []})


class CliTests(unittest.TestCase):
    def test_output_is_new_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            output = work / 'plan.json'
            first = run_cli('--snapshot', str(SNAPSHOT), '--roster', str(ROSTER),
                            '--output', str(output))
            self.assertEqual(first.returncode, 0, first.stderr)
            original = output.read_bytes()
            second = run_cli('--snapshot', str(SNAPSHOT), '--roster', str(ROSTER),
                             '--output', str(output))
            self.assertEqual(second.returncode, 2)
            self.assertEqual(output.read_bytes(), original)

            target = work / 'real.json'
            target.write_text('keep\n', encoding='utf-8')
            link = work / 'link.json'
            link.symlink_to(target)
            third = run_cli('--snapshot', str(SNAPSHOT), '--roster', str(ROSTER),
                            '--output', str(link))
            self.assertEqual(third.returncode, 2)
            self.assertEqual(target.read_text(encoding='utf-8'), 'keep\n')

    def test_bad_input_leaves_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            bad = write_json(work / 'bad.json', {'schema_version': 2,
                                                 'head': 0, 'buckets': []})
            output = work / 'plan.json'
            result = run_cli('--snapshot', str(bad), '--roster', str(ROSTER),
                             '--output', str(output))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertFalse(output.exists())

    def test_duplicate_json_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            duplicate = work / 'snapshot.json'
            duplicate.write_text(
                '{"schema_version":1,"head":1,"head":2,"buckets":[]}',
                encoding='utf-8')
            output = work / 'plan.json'
            result = run_cli('--snapshot', str(duplicate),
                             '--roster', str(ROSTER), '--output', str(output))
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()

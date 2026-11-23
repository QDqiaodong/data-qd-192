"""周报联发审查测试：验收场景、独立整数穷举对照、受限进程容量与边界。"""
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
from sequence_digest.release import ReleaseError, review

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'

DAY_X = '2026-11-10'
DAY_Y = '2026-11-11'


def run_module(module, *args, cpu_seconds=None, address_space=None):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(SRC)

    def preexec():
        if address_space is not None:
            resource.setrlimit(resource.RLIMIT_AS,
                               (address_space, address_space))
        if cpu_seconds is not None:
            resource.setrlimit(resource.RLIMIT_CPU,
                               (cpu_seconds, cpu_seconds))

    limited = cpu_seconds is not None or address_space is not None
    return subprocess.run(
        [sys.executable, '-P', '-m', module, *args],
        cwd=str(ROOT), env=env, text=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        preexec_fn=preexec if limited else None)


def run_cli(*args, **kwargs):
    return run_module('sequence_digest.release_cli', *args, **kwargs)


def run_ledger(*args):
    return run_module('sequence_digest.ledger_cli', *args)


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8')
    return path


def summary(fragments=1, bases=120):
    return {'bases': bases, 'fragment_bases': bases, 'fragments': fragments,
            'unique_cuts': 0, 'recognition_hits': 0}


def snapshot_payload(matrix, day_x=DAY_X, day_y=DAY_Y):
    """matrix: {course_id: (x_count, y_count)}，全零格不列入快照。"""
    buckets = []
    for course_id in sorted(matrix):
        for day, count in ((day_x, matrix[course_id][0]),
                           (day_y, matrix[course_id][1])):
            if count:
                buckets.append({
                    'course_id': course_id, 'day': day,
                    'counts': {'single': count, 'light': 0, 'heavy': 0},
                    'points': count})
    return {'schema_version': 1, 'head': 1, 'buckets': buckets}


def layout_payload(protected, pages, courses=('A', 'B'), days=(DAY_X, DAY_Y),
                   tier='single'):
    return {'courses': list(courses), 'days': list(days), 'tier': tier,
            'protected': protected, 'pages': pages}


def page(page_id, cells=(), course_totals=(), day_totals=()):
    return {'page_id': page_id, 'cells': list(cells),
            'course_totals': list(course_totals),
            'day_totals': list(day_totals)}


class AcceptanceTests(unittest.TestCase):
    """验收第 1 条：真实台账快照上的联发拒绝与放行。"""

    def _real_snapshot(self, directory):
        records = []
        serial = 0
        for course_id, day, count in (
                ('A', DAY_X, 2), ('A', DAY_Y, 8),
                ('B', DAY_X, 7), ('B', DAY_Y, 13)):
            for _ in range(count):
                serial += 1
                records.append({'submission_id': f's{serial:03d}',
                                'revision': 1, 'op': 'put',
                                'course_id': course_id, 'day': day,
                                'summary': summary(fragments=1)})
        batch = write_json(Path(directory) / 'batch.json',
                           {'records': records})
        store = Path(directory) / 'ledger.json'
        self.assertEqual(run_ledger('apply', '--store', str(store),
                                    '--source', str(batch)).returncode, 0)
        result = run_ledger('snapshot', '--store', str(store))
        self.assertEqual(result.returncode, 0)
        snapshot_path = Path(directory) / 'snapshot.json'
        snapshot_path.write_text(result.stdout, encoding='utf-8')
        return snapshot_path

    def test_joint_release_forces_only_ax(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = self._real_snapshot(tmp)
            layout = layout_payload(
                protected=[['A', DAY_X]],
                pages=[page('p1', course_totals=['A', 'B']),
                       page('p2', cells=[['B', DAY_Y]],
                           day_totals=[DAY_X, DAY_Y])])
            layout_path = write_json(Path(tmp) / 'layout.json', layout)
            output = Path(tmp) / 'package.json'
            result = run_cli('--snapshot', str(snapshot_path),
                             '--layout', str(layout_path),
                             '--output', str(output))
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertEqual(json.loads(result.stderr),
                             {'forced': [['A', DAY_X]]})
            self.assertEqual(len(result.stderr.strip().splitlines()), 1)
            self.assertFalse(output.exists())

    def test_each_page_alone_does_not_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = self._real_snapshot(tmp)
            for pages in (
                    [page('p1', course_totals=['A', 'B']), page('p2')],
                    [page('p1'), page('p2', cells=[['B', DAY_Y]],
                                      day_totals=[DAY_X, DAY_Y])]):
                layout = layout_payload(protected=[['A', DAY_X]], pages=pages)
                layout_path = write_json(Path(tmp) / 'layout.json', layout)
                output = Path(tmp) / 'package.json'
                if output.exists():
                    output.unlink()
                result = run_cli('--snapshot', str(snapshot_path),
                                 '--layout', str(layout_path),
                                 '--output', str(output))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(output.exists())

    def test_dropping_one_cell_allows_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = self._real_snapshot(tmp)
            layout = layout_payload(
                protected=[['A', DAY_X]],
                pages=[page('p1', course_totals=['A', 'B']),
                       page('p2', day_totals=[DAY_X, DAY_Y])])
            layout_path = write_json(Path(tmp) / 'layout.json', layout)
            output = Path(tmp) / 'package.json'
            result = run_cli('--snapshot', str(snapshot_path),
                             '--layout', str(layout_path),
                             '--output', str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            package = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(set(package),
                             {'schema_version', 'tier', 'courses', 'days',
                              'pages'})
            self.assertEqual(package['schema_version'], 1)
            self.assertEqual(package['tier'], 'single')
            self.assertEqual(package['courses'], ['A', 'B'])
            self.assertEqual(package['days'], [DAY_X, DAY_Y])
            self.assertNotIn('protected', package)
            self.assertNotIn('head', package)
            first, second = package['pages']
            self.assertEqual(first['page_id'], 'p1')
            self.assertEqual(first['course_totals'],
                             [{'course_id': 'A', 'count': 10},
                              {'course_id': 'B', 'count': 20}])
            self.assertEqual(second['day_totals'],
                             [{'day': DAY_X, 'count': 9},
                              {'day': DAY_Y, 'count': 21}])


# --- 独立整数穷举对照：不复用 release 的任何判定逻辑 -------------------------

def oracle_forced(actual, upper, pub_cells, pub_rows, pub_cols, protected):
    """枚举全部满足刊登约束的整数表，返回可能值不足两个的受保护格。

    只用于小表对照；按行优先顺序逐格枚举，用行/列剩余合计剪枝，叶子处再
    精确核对全部已公开合计。行、列合计都未公开的未刊登格是完全自由的
    （只翻动它自己 ±1 仍满足全部约束），枚举时固定在真实值、并直接记为
    至少两个可能值；这不影响其他格的可行集合。
    """
    rows = len(actual)
    cols = len(actual[0])
    cells = [(i, j) for i in range(rows) for j in range(cols)]
    free_free = {(i, j) for i in range(rows) for j in range(cols)
                 if (i, j) not in pub_cells
                 and i not in pub_rows and j not in pub_cols}
    seen = {cell: set() for cell in protected}
    for cell in protected:
        if cell in free_free:
            seen[cell] = {0, 1}
    grid = [[0] * cols for _ in range(rows)]
    row_start = [sum(actual[i]) if i in pub_rows else None
                 for i in range(rows)]
    col_start = [sum(actual[i][j] for i in range(rows)) if j in pub_cols
                 else None for j in range(cols)]

    def rec(k, row_rem, col_rem):
        if k == len(cells):
            if any(rem not in (0, None) for rem in row_rem):
                return
            if any(rem not in (0, None) for rem in col_rem):
                return
            for cell in protected:
                if cell not in free_free:
                    seen[cell].add(grid[cell[0]][cell[1]])
            return
        i, j = cells[k]
        if (i, j) in pub_cells or (i, j) in free_free:
            lo = hi = actual[i][j]
        else:
            lo, hi = 0, upper
        if row_rem[i] is not None:
            tail_min = tail_max = 0
            for jj in range(j + 1, cols):
                if (i, jj) in pub_cells:
                    tail_min += actual[i][jj]
                    tail_max += actual[i][jj]
                else:
                    tail_max += upper
            hi = min(hi, row_rem[i] - tail_min)
            lo = max(lo, row_rem[i] - tail_max)
        if col_rem[j] is not None:
            tail_min = tail_max = 0
            for ii in range(i + 1, rows):
                if (ii, j) in pub_cells:
                    tail_min += actual[ii][j]
                    tail_max += actual[ii][j]
                else:
                    tail_max += upper
            hi = min(hi, col_rem[j] - tail_min)
            lo = max(lo, col_rem[j] - tail_max)
        for value in range(lo, hi + 1):
            grid[i][j] = value
            next_row = row_rem if row_rem[i] is None else \
                row_rem[:i] + [row_rem[i] - value] + row_rem[i + 1:]
            next_col = col_rem if col_rem[j] is None else \
                col_rem[:j] + [col_rem[j] - value] + col_rem[j + 1:]
            rec(k + 1, next_row, next_col)
        grid[i][j] = 0

    rec(0, row_start, col_start)
    return {cell for cell in protected if len(seen[cell]) < 2}


def review_forced(actual, upper, pub_cells, pub_rows, pub_cols, protected,
                  tier='single'):
    """用被测实现求受保护格集合（行列均用 c{i}/2026-11-1{j} 编号）。"""
    rows = len(actual)
    cols = len(actual[0])
    courses = [f'c{i}' for i in range(rows)]
    days = [f'2026-11-{10 + j:02d}' for j in range(cols)]
    buckets = []
    for i in range(rows):
        for j in range(cols):
            if actual[i][j]:
                counts = {'single': 0, 'light': 0, 'heavy': 0}
                counts[tier] = actual[i][j]
                buckets.append({'course_id': courses[i], 'day': days[j],
                                'counts': counts,
                                'points': actual[i][j]
                                * {'single': 1, 'light': 3, 'heavy': 6}[tier]})
    snapshot = {'schema_version': 1, 'head': 0, 'buckets': buckets}
    layout = {
        'courses': courses, 'days': days, 'tier': tier,
        'protected': [[courses[i], days[j]] for i, j in protected],
        'pages': [
            {'page_id': 'p1',
             'cells': [[courses[i], days[j]] for i, j in sorted(pub_cells)],
             'course_totals': [courses[i] for i in sorted(pub_rows)],
             'day_totals': []},
            {'page_id': 'p2', 'cells': [], 'course_totals': [],
             'day_totals': [days[j] for j in sorted(pub_cols)]},
        ],
    }
    result = review(snapshot, layout)
    return {(courses.index(course), days.index(day))
            for course, day in result['forced']}


class OracleCrossCheckTests(unittest.TestCase):
    """验收第 2 条：小表独立整数穷举对照。"""

    UPPER = 1000000

    def check(self, actual, pub_cells, pub_rows, pub_cols, protected=None,
              upper=UPPER, tier='single'):
        if protected is None:
            protected = [(i, j) for i in range(len(actual))
                         for j in range(len(actual[0]))]
        expected = oracle_forced(actual, upper, pub_cells, pub_rows,
                                 pub_cols, protected)
        got = review_forced(actual, upper, pub_cells, pub_rows, pub_cols,
                            protected, tier=tier)
        self.assertEqual(got, expected,
                         (actual, pub_cells, pub_rows, pub_cols, protected))
        return got

    def test_direct_cell_publication_is_forced(self):
        got = self.check([[2, 8], [7, 13]],
                         pub_cells={(0, 0)}, pub_rows=set(), pub_cols=set())
        self.assertEqual(got, {(0, 0)})

    def test_positive_value_without_compensation_is_forced(self):
        got = self.check([[5]], pub_cells=set(), pub_rows={0},
                         pub_cols=set())
        self.assertEqual(got, {(0, 0)})

    def test_zero_total_fixes_unpublished_cells(self):
        got = self.check([[0, 0], [3, 4]],
                         pub_cells=set(), pub_rows={0}, pub_cols=set())
        self.assertEqual(got, {(0, 0), (0, 1)})

    def test_partial_axis_totals_still_allow_alternatives(self):
        # 只公开课程合计、不公开日期合计时，2x2 每格都还有替代值。
        got = self.check([[2, 8], [7, 13]],
                         pub_cells=set(), pub_rows={0, 1}, pub_cols=set())
        self.assertEqual(got, set())

    def test_high_value_ceiling_fixes_both_cells(self):
        # single 档一门课两天，课程合计 2000000：两格都顶到 1000000。
        got = self.check([[1000000, 1000000]],
                         pub_cells=set(), pub_rows={0}, pub_cols=set())
        self.assertEqual(got, {(0, 0), (0, 1)})

    def test_joint_pages_fix_cell_that_single_page_cannot(self):
        # 与验收第 1 条同构：合计联合 + 一格明细固定 A/x。
        got = self.check([[2, 8], [7, 13]],
                         pub_cells={(1, 1)}, pub_rows={0, 1}, pub_cols={0, 1},
                         protected=[(0, 0)])
        self.assertEqual(got, {(0, 0)})

    def test_random_small_tables_match_oracle(self):
        rng = random.Random(20261123)
        checked = 0
        for _ in range(120):
            rows = rng.randint(1, 3)
            cols = rng.randint(1, 4)
            tier = rng.choice(['single', 'light', 'heavy'])
            upper = 1000000 // {'single': 1, 'light': 3, 'heavy': 6}[tier]
            actual = [[rng.randint(0, 4) for _ in range(cols)]
                      for _ in range(rows)]
            all_cells = [(i, j) for i in range(rows) for j in range(cols)]
            pub_cells = {cell for cell in all_cells if rng.random() < 0.2}
            pub_rows = {i for i in range(rows) if rng.random() < 0.6}
            pub_cols = {j for j in range(cols) if rng.random() < 0.6}
            protected = [cell for cell in all_cells if rng.random() < 0.7]
            if not protected:
                continue
            self.check(actual, pub_cells, pub_rows, pub_cols, protected,
                       upper=upper, tier=tier)
            checked += 1
        self.assertGreater(checked, 80)

    def test_light_and_heavy_tier_bounds(self):
        # light 上限 333333、heavy 上限 166666：触顶合计同样固定两格。
        self.check([[333333, 333333]], pub_cells=set(), pub_rows={0},
                   pub_cols=set(), upper=333333, tier='light')
        self.check([[166666, 166666]], pub_cells=set(), pub_rows={0},
                   pub_cols=set(), upper=166666, tier='heavy')


class CapacityAcceptanceTests(unittest.TestCase):
    """验收第 3 条：256 课 × 32 日在 128 MiB / 20 CPU 秒限额内跑两遍。"""

    ADDRESS_SPACE = 128 * 1024 * 1024
    CPU_SECONDS = 20

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'release_bench', ROOT / 'tools' / 'release_bench.py')
        self.bench = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bench)

    def test_full_and_trimmed_layouts_under_limits(self):
        bench = self.bench
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            snapshot_path = write_json(work / 'snapshot.json',
                                       bench.build_snapshot())
            self.assertLess(snapshot_path.stat().st_size, 8_000_000)

            full_layout = write_json(work / 'layout-full.json',
                                     bench.build_layout(full=True))
            self.assertLess(full_layout.stat().st_size, 8_000_000)
            blocked_output = work / 'package-full.json'
            blocked = run_cli('--snapshot', str(snapshot_path),
                              '--layout', str(full_layout),
                              '--output', str(blocked_output),
                              cpu_seconds=self.CPU_SECONDS,
                              address_space=self.ADDRESS_SPACE)
            self.assertEqual(blocked.returncode, 3, blocked.stderr[:200])
            self.assertEqual(json.loads(blocked.stderr),
                             {'forced': bench.expected_forced()})
            self.assertEqual(len(json.loads(blocked.stderr)['forced']), 32)
            self.assertFalse(blocked_output.exists())

            trimmed_layout = write_json(work / 'layout-trimmed.json',
                                        bench.build_layout(full=False))
            released_output = work / 'package-trimmed.json'
            released = run_cli('--snapshot', str(snapshot_path),
                               '--layout', str(trimmed_layout),
                               '--output', str(released_output),
                               cpu_seconds=self.CPU_SECONDS,
                               address_space=self.ADDRESS_SPACE)
            self.assertEqual(released.returncode, 0, released.stderr[:200])
            bench.verify_package(
                json.loads(released_output.read_text(encoding='utf-8')))


class BoundaryTests(unittest.TestCase):
    """验收第 2 条余项：乱序、非法引用、既有目标与输出字段边界。"""

    def _layout_and_snapshot(self):
        snapshot = snapshot_payload({'A': (2, 8), 'B': (7, 13)})
        layout = layout_payload(
            protected=[['A', DAY_X]],
            pages=[page('p1', course_totals=['A', 'B']),
                   page('p2', day_totals=[DAY_X, DAY_Y])])
        return snapshot, layout

    def test_input_order_does_not_change_output_bytes(self):
        snapshot, layout = self._layout_and_snapshot()
        baseline = json.dumps(review(snapshot, layout)['package'],
                              sort_keys=True, separators=(',', ':'))
        rng = random.Random(11)
        for _ in range(6):
            shuffled = json.loads(json.dumps(layout))
            rng.shuffle(shuffled['courses'])
            rng.shuffle(shuffled['days'])
            rng.shuffle(shuffled['protected'])
            rng.shuffle(shuffled['pages'])
            for item in shuffled['pages']:
                rng.shuffle(item['cells'])
                rng.shuffle(item['course_totals'])
                rng.shuffle(item['day_totals'])
            again = json.dumps(review(snapshot, shuffled)['package'],
                               sort_keys=True, separators=(',', ':'))
            self.assertEqual(again, baseline)

    def test_references_outside_rectangle_are_rejected(self):
        snapshot, layout = self._layout_and_snapshot()
        for mutate in (
                lambda lay: lay['pages'][0]['cells'].append(['C', DAY_X]),
                lambda lay: lay['pages'][0]['cells'].append(['A', '2026-01-01']),
                lambda lay: lay['pages'][0]['course_totals'].append('C'),
                lambda lay: lay['pages'][1]['day_totals'].append('2026-01-01'),
                lambda lay: lay['protected'].append(['C', DAY_X]),
                lambda lay: lay['protected'].append(['A', '2026-01-01'])):
            bad = json.loads(json.dumps(layout))
            mutate(bad)
            with self.assertRaises(ReleaseError):
                review(snapshot, bad)

    def test_duplicates_are_rejected(self):
        snapshot, layout = self._layout_and_snapshot()
        bad = json.loads(json.dumps(layout))
        bad['protected'].append(['A', DAY_X])
        with self.assertRaises(ReleaseError):
            review(snapshot, bad)
        bad = json.loads(json.dumps(layout))
        bad['pages'][0]['course_totals'].append('A')
        with self.assertRaises(ReleaseError):
            review(snapshot, bad)
        bad = json.loads(json.dumps(layout))
        bad['pages'][1]['day_totals'].append(DAY_X)
        with self.assertRaises(ReleaseError):
            review(snapshot, bad)
        bad = json.loads(json.dumps(layout))
        bad['pages'].append(page('p1'))
        with self.assertRaises(ReleaseError):
            review(snapshot, bad)
        bad = json.loads(json.dumps(layout))
        bad['pages'][0]['cells'].append(['A', DAY_X])
        bad['pages'][0]['cells'].append(['A', DAY_X])
        with self.assertRaises(ReleaseError):
            review(snapshot, bad)

    def test_same_number_may_repeat_across_pages(self):
        snapshot, layout = self._layout_and_snapshot()
        # 同一合计在不同附件重复刊登是合法输入，且不产生新约束。
        layout['pages'][0]['day_totals'].append(DAY_X)
        layout['pages'][1]['course_totals'].append('A')
        result = review(snapshot, layout)
        self.assertEqual(result['forced'], [])

    def test_structural_rejections(self):
        snapshot, layout = self._layout_and_snapshot()
        with self.assertRaises(ReleaseError):
            review(snapshot, {'courses': ['A'], 'days': [DAY_X], 'tier':
                              'single', 'protected': [['A', DAY_X]]})
        bad = json.loads(json.dumps(layout))
        bad['tier'] = 'mixed'
        with self.assertRaises(ReleaseError):
            review(snapshot, bad)
        bad = json.loads(json.dumps(layout))
        bad['extra'] = 1
        with self.assertRaises(ReleaseError):
            review(snapshot, bad)
        bad = json.loads(json.dumps(layout))
        bad['pages'] = [page('only')]
        with self.assertRaises(ReleaseError):
            review(snapshot, bad)
        bad = json.loads(json.dumps(layout))
        bad['protected'] = []
        with self.assertRaises(ReleaseError):
            review(snapshot, bad)
        bad = json.loads(json.dumps(layout))
        bad['days'] = [DAY_X, '2026-13-01']
        with self.assertRaises(ReleaseError):
            review(snapshot, bad)

    def test_snapshot_violations_are_rejected(self):
        _, layout = self._layout_and_snapshot()
        with self.assertRaises(ValueError):
            review({'schema_version': 1, 'head': 0, 'buckets': [
                {'course_id': 'A', 'day': DAY_X,
                 'counts': {'single': 1, 'light': 0, 'heavy': 0},
                 'points': 2}]}, layout)
        with self.assertRaises(ValueError):
            review({'schema_version': 1, 'head': 0, 'buckets': [
                {'course_id': 'A', 'day': DAY_X,
                 'counts': {'single': 0, 'light': 0, 'heavy': 0},
                 'points': 0}]}, layout)
        with self.assertRaises(ValueError):
            review({'schema_version': 1, 'head': 0, 'buckets': [
                {'course_id': 'A', 'day': DAY_X,
                 'counts': {'single': True, 'light': 0, 'heavy': 0},
                 'points': 1}]}, layout)
        with self.assertRaises(ValueError):
            review({'schema_version': 2, 'head': 0, 'buckets': []}, layout)

    def test_out_of_rectangle_buckets_are_ignored(self):
        snapshot, layout = self._layout_and_snapshot()
        snapshot['buckets'].append({
            'course_id': 'Z', 'day': '2026-01-01',
            'counts': {'single': 9, 'light': 0, 'heavy': 0}, 'points': 9})
        snapshot['buckets'].append({
            'course_id': 'A', 'day': '2026-01-01',
            'counts': {'single': 9, 'light': 0, 'heavy': 0}, 'points': 9})
        result = review(snapshot, layout)
        self.assertEqual(result['forced'], [])
        totals = {row['course_id']: row['count']
                  for row in result['package']['pages'][0]['course_totals']}
        self.assertEqual(totals, {'A': 10, 'B': 20})

    def test_forced_cells_sorted_and_values_not_leaked(self):
        # 两门课各只有一天：课程合计直接固定两格；输入乱序仍按身份升序报告。
        snapshot = {'schema_version': 1, 'head': 0, 'buckets': [
            {'course_id': 'B', 'day': DAY_X,
             'counts': {'single': 5, 'light': 0, 'heavy': 0}, 'points': 5},
            {'course_id': 'A', 'day': DAY_X,
             'counts': {'single': 3, 'light': 0, 'heavy': 0}, 'points': 3}]}
        layout = {'courses': ['B', 'A'], 'days': [DAY_X], 'tier': 'single',
                  'protected': [['B', DAY_X], ['A', DAY_X]],
                  'pages': [page('p1', course_totals=['B', 'A']), page('p2')]}
        result = review(snapshot, layout)
        self.assertEqual(result['forced'],
                         [['A', DAY_X], ['B', DAY_X]])
        self.assertNotIn('3', json.dumps(result['forced']))
        self.assertNotIn('5', json.dumps(result['forced']))


class CliTests(unittest.TestCase):
    def _inputs(self, work):
        snapshot = write_json(work / 'snapshot.json',
                              snapshot_payload({'A': (2, 8), 'B': (7, 13)}))
        layout = write_json(work / 'layout.json', layout_payload(
            protected=[['A', DAY_X]],
            pages=[page('p1', course_totals=['A', 'B']),
                   page('p2', day_totals=[DAY_X, DAY_Y])]))
        return snapshot, layout

    def test_output_is_new_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            snapshot, layout = self._inputs(work)
            output = work / 'package.json'
            first = run_cli('--snapshot', str(snapshot), '--layout', str(layout),
                            '--output', str(output))
            self.assertEqual(first.returncode, 0, first.stderr)
            original = output.read_bytes()
            second = run_cli('--snapshot', str(snapshot), '--layout', str(layout),
                             '--output', str(output))
            self.assertEqual(second.returncode, 2)
            self.assertEqual(output.read_bytes(), original)

            target = work / 'real.json'
            target.write_text('keep\n', encoding='utf-8')
            link = work / 'link.json'
            link.symlink_to(target)
            third = run_cli('--snapshot', str(snapshot), '--layout', str(layout),
                            '--output', str(link))
            self.assertEqual(third.returncode, 2)
            self.assertEqual(target.read_text(encoding='utf-8'), 'keep\n')

    def test_bad_input_leaves_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            bad = write_json(work / 'bad.json',
                             {'schema_version': 2, 'head': 0, 'buckets': []})
            _, layout = self._inputs(work)
            output = work / 'package.json'
            result = run_cli('--snapshot', str(bad), '--layout', str(layout),
                             '--output', str(output))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, '')
            self.assertFalse(output.exists())

    def test_duplicate_json_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            _, layout = self._inputs(work)
            duplicate = work / 'snapshot.json'
            duplicate.write_text(
                '{"schema_version":1,"head":1,"head":2,"buckets":[]}',
                encoding='utf-8')
            output = work / 'package.json'
            result = run_cli('--snapshot', str(duplicate),
                             '--layout', str(layout), '--output', str(output))
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())

    def test_forced_exit_leaves_no_output_and_one_stderr_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            snapshot = write_json(work / 'snapshot.json',
                                  snapshot_payload({'A': (2, 8), 'B': (7, 13)}))
            layout = write_json(work / 'layout.json', layout_payload(
                protected=[['A', DAY_X]],
                pages=[page('p1', course_totals=['A', 'B']),
                       page('p2', cells=[['B', DAY_Y]],
                            day_totals=[DAY_X, DAY_Y])]))
            output = work / 'package.json'
            result = run_cli('--snapshot', str(snapshot), '--layout', str(layout),
                             '--output', str(output))
            self.assertEqual(result.returncode, 3)
            self.assertEqual(result.stdout, '')
            self.assertFalse(output.exists())
            line = result.stderr.strip()
            self.assertEqual(len(line.splitlines()), 1)
            self.assertEqual(json.loads(line), {'forced': [['A', DAY_X]]})


if __name__ == '__main__':
    unittest.main()

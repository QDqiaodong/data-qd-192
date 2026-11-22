"""复核产能排班的容量验收复现器。

生成第 1 条更正后结构的 128 份互不共享班次与课程的副本，每份把所有点数
（快照三档计数、班次容量、草案与确认点）乘以 1000、编号加互不冲突的前缀，
同一计划期；再把两份输入写到磁盘，用受限进程（256 MiB 虚拟地址空间）运行
真实 CLI 从读取到写出，最后核对逐桶、逐分配结果与单副本一致。

用法：

    python3 -P tools/staff_capacity.py --directory run/staff-capacity

只依赖标准库；输入生成在受限进程之外，CLI 本身不转交子进程或外部服务。
"""
import argparse
import json
import os
from pathlib import Path
import resource
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
COPIES = 128
SCALE = 1000
ADDRESS_SPACE = 256 * 1024 * 1024
EXPECTED_COVERED = 9 * SCALE * COPIES
EXPECTED_RETAINED = 3 * SCALE * COPIES

SINGLE = {
    'snapshot': {
        'schema_version': 1, 'head': 2,
        'buckets': [
            {'course_id': 'Blue', 'day': '2026-11-10',
             'counts': {'single': 0, 'light': 0, 'heavy': 1}, 'points': 6},
            {'course_id': 'Red', 'day': '2026-11-11',
             'counts': {'single': 0, 'light': 0, 'heavy': 1}, 'points': 6},
        ],
    },
    'roster': {
        'start_day': '2026-11-10', 'end_day': '2026-11-11',
        'shifts': [
            {'shift_id': 'F', 'day': '2026-11-10',
             'courses': ['Blue', 'Red'], 'capacity': 6},
            {'shift_id': 'B0', 'day': '2026-11-10',
             'courses': ['Blue'], 'capacity': 6},
            {'shift_id': 'B1', 'day': '2026-11-10',
             'courses': ['Blue'], 'capacity': 6},
        ],
        'bookings': [
            {'shift_id': 'F', 'course_id': 'Blue', 'day': '2026-11-10',
             'points': 6, 'locked_points': 3},
            {'shift_id': 'B1', 'course_id': 'Blue', 'day': '2026-11-10',
             'points': 3, 'locked_points': 0},
        ],
    },
}


def _prefix(index):
    return f'k{index:03d}_'


def build_batch():
    buckets, shifts, bookings = [], [], []
    for index in range(COPIES):
        prefix = _prefix(index)
        for bucket in SINGLE['snapshot']['buckets']:
            scaled = dict(bucket)
            scaled['course_id'] = prefix + bucket['course_id']
            scaled['counts'] = {tier: count * SCALE
                                for tier, count in bucket['counts'].items()}
            scaled['points'] = bucket['points'] * SCALE
            buckets.append(scaled)
        for shift in SINGLE['roster']['shifts']:
            scaled = dict(shift)
            scaled['shift_id'] = prefix + shift['shift_id']
            scaled['courses'] = [prefix + course for course in shift['courses']]
            scaled['capacity'] = shift['capacity'] * SCALE
            shifts.append(scaled)
        for booking in SINGLE['roster']['bookings']:
            scaled = dict(booking)
            scaled['shift_id'] = prefix + booking['shift_id']
            scaled['course_id'] = prefix + booking['course_id']
            scaled['points'] = booking['points'] * SCALE
            scaled['locked_points'] = booking['locked_points'] * SCALE
            bookings.append(scaled)
    return ({'schema_version': SINGLE['snapshot']['schema_version'],
             'head': SINGLE['snapshot']['head'], 'buckets': buckets},
            {'start_day': SINGLE['roster']['start_day'],
             'end_day': SINGLE['roster']['end_day'],
             'shifts': shifts, 'bookings': bookings})


def _limit_address_space():
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE, ADDRESS_SPACE))


def run(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    snapshot, roster = build_batch()
    snapshot_path = directory / 'snapshot.json'
    roster_path = directory / 'roster.json'
    output_path = directory / 'plan.json'
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False) + '\n', encoding='utf-8')
    roster_path.write_text(
        json.dumps(roster, ensure_ascii=False) + '\n', encoding='utf-8')
    if output_path.exists():
        raise SystemExit(f'refusing to overwrite {output_path}')

    env = dict(os.environ)
    env['PYTHONPATH'] = str(SRC)
    completed = subprocess.run(
        [sys.executable, '-P', '-m', 'sequence_digest.staff_cli',
         '--snapshot', str(snapshot_path), '--roster', str(roster_path),
         '--output', str(output_path)],
        cwd=str(ROOT), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        preexec_fn=_limit_address_space)
    if completed.returncode != 0:
        raise SystemExit(f'CLI failed: {completed.stderr}')

    plan = json.loads(output_path.read_text(encoding='utf-8'))
    problems = check(plan, snapshot, roster)
    if problems:
        raise SystemExit('capacity check failed:\n' + '\n'.join(problems))
    print(json.dumps({
        'copies': COPIES, 'scale': SCALE,
        'covered_points': plan['covered_points'],
        'retained_provisional_points': plan['retained_provisional_points'],
        'address_space_limit': ADDRESS_SPACE,
        'output': str(output_path),
    }, ensure_ascii=False, sort_keys=True))


def check(plan, snapshot, roster):
    """逐桶、逐分配核对总覆盖/保留，以及每份副本与单副本结果一致。"""
    problems = []
    if plan['covered_points'] != EXPECTED_COVERED:
        problems.append(
            f'covered {plan["covered_points"]} != {EXPECTED_COVERED}')
    if plan['retained_provisional_points'] != EXPECTED_RETAINED:
        problems.append(
            f'retained {plan["retained_provisional_points"]} '
            f'!= {EXPECTED_RETAINED}')
    rows = {(row['shift_id'], row['course_id'], row['day']): row
            for row in plan['assignments']}
    expected = {
        ('F', 'Blue', '2026-11-10'): (3, 3),
        ('F', 'Red', '2026-11-11'): (3, 0),
        ('B1', 'Blue', '2026-11-10'): (3, 0),
    }
    for index in range(COPIES):
        prefix = _prefix(index)
        for (shift_id, course, day), (points, locked) in expected.items():
            row = rows.get((prefix + shift_id, prefix + course, day))
            if row is None:
                problems.append(f'missing assignment {index} {shift_id}')
                continue
            if row['points'] != points * SCALE \
                    or row['locked_points'] != locked * SCALE:
                problems.append(f'bad assignment {index} {shift_id}')
        if (prefix + 'B0', prefix + 'Blue', '2026-11-10') in rows:
            problems.append(f'B0 must stay idle in copy {index}')
    covered_total = sum(row['covered_points'] for row in plan['buckets'])
    if covered_total != plan['covered_points']:
        problems.append('bucket covered points do not sum to top level')
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='生成并受限运行第 2 条的 128 副本容量验收。')
    parser.add_argument('--directory', required=True, help='生成目录')
    args = parser.parse_args(argv)
    run(args.directory)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

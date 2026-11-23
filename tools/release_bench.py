"""周报联发审查的受限进程验收（验收第 3 条）。

在受限进程**之外**直接生成合法 v1 快照与两份版面：single 档 256 门课 × 32 个
结算日，前 255 门每格 500000，末门每格为零（快照不列末门的桶）。保护全部
8192 格；第一页刊登全部课程合计，第二页刊登全部日期合计，无明细。真实 CLI
在单进程 ``RLIMIT_AS=128 MiB``、``RLIMIT_CPU=20`` CPU 秒下从读入跑到结束，
不把计算转给子进程或服务：

1. 完整版面：须精确拒绝末门的全部 32 格且不误报其他课程（退出 3，无输出）。
2. 从第一页撤下末门与首门的课程合计（其余栏目不变）：须在同一限额下发布
   完整包（退出 0）。此时首门某日的一个计数可移到末门同日，其他正值行仍可
   在有公布合计的行日之间成对调整。

用法：

    python3 -P tools/release_bench.py --directory run/release-bench

只依赖标准库；生成与断言不计入 CLI 限额。
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
COURSES = 256
DAYS = 32
VALUE = 500000
ADDRESS_SPACE = 128 * 1024 * 1024
CPU_SECONDS = 20
LAST_COURSE_TOTAL = 0
COURSE_TOTAL = DAYS * VALUE          # 16000000
DAY_TOTAL = (COURSES - 1) * VALUE    # 127500000


def course_ids():
    return [f'c{index:03d}' for index in range(COURSES)]


def day_ids():
    # 2026-11-01..2026-11-30 与 2026-12-01..2026-12-02，字典序即时间序。
    return ([f'2026-11-{day:02d}' for day in range(1, 31)]
            + ['2026-12-01', '2026-12-02'])


def build_snapshot():
    """末门（c255）每格为零，快照不列它的桶。"""
    days = day_ids()
    buckets = []
    for course in course_ids()[:COURSES - 1]:
        for day in days:
            buckets.append({'course_id': course, 'day': day,
                            'counts': {'single': VALUE, 'light': 0, 'heavy': 0},
                            'points': VALUE})
    return {'schema_version': 1, 'head': 1, 'buckets': buckets}


def build_layout(full=True):
    """完整版面刊登全部课程合计；``full=False`` 撤下首门与末门的课程合计。"""
    courses = course_ids()
    days = day_ids()
    kept = courses if full else courses[1:COURSES - 1]
    return {
        'courses': courses,
        'days': days,
        'tier': 'single',
        'protected': [[course, day] for course in courses for day in days],
        'pages': [
            {'page_id': 'p1', 'cells': [], 'course_totals': kept,
             'day_totals': []},
            {'page_id': 'p2', 'cells': [], 'course_totals': [],
             'day_totals': days},
        ],
    }


def expected_forced():
    """完整版面只固定末门的 32 格，按课程、日期升序。"""
    return [['c255', day] for day in day_ids()]


def verify_package(package):
    """逐栏目核对发布包：只含版面点名的信息，且不含保护清单/游标。"""
    courses = course_ids()
    days = day_ids()
    assert set(package) == {'schema_version', 'tier', 'courses', 'days',
                            'pages'}, package.keys()
    assert package['schema_version'] == 1
    assert package['tier'] == 'single'
    assert package['courses'] == courses
    assert package['days'] == days
    pages = package['pages']
    assert [page['page_id'] for page in pages] == ['p1', 'p2']
    for page in pages:
        assert set(page) == {'page_id', 'cells', 'course_totals',
                             'day_totals'}, page.keys()
    first, second = pages
    assert first['cells'] == [] and first['day_totals'] == []
    kept = courses[1:COURSES - 1]
    assert [row['course_id'] for row in first['course_totals']] == kept
    for row in first['course_totals']:
        assert set(row) == {'course_id', 'count'}, row
        assert row['count'] == COURSE_TOTAL, row
    assert second['cells'] == [] and second['course_totals'] == []
    assert [row['day'] for row in second['day_totals']] == days
    for row in second['day_totals']:
        assert set(row) == {'day', 'count'}, row
        assert row['count'] == DAY_TOTAL, row
    assert 'protected' not in package and 'head' not in package


def _limits():
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE, ADDRESS_SPACE))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))


def _run_cli(snapshot, layout, output):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(SRC)
    return subprocess.run(
        [sys.executable, '-P', '-m', 'sequence_digest.release_cli',
         '--snapshot', str(snapshot), '--layout', str(layout),
         '--output', str(output)],
        cwd=str(ROOT), env=env, text=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=_limits)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='生成并在 128 MiB / 20 CPU 秒限额内运行第 3 条联发验收。')
    parser.add_argument('--directory', required=True, help='生成目录')
    args = parser.parse_args(argv)
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)

    snapshot_path = directory / 'snapshot.json'
    snapshot_path.write_text(json.dumps(build_snapshot(), ensure_ascii=False)
                             + '\n', encoding='utf-8')

    # 第一跑：完整版面，须精确拒绝末门 32 格。
    layout_path = directory / 'layout-full.json'
    layout_path.write_text(json.dumps(build_layout(full=True),
                                      ensure_ascii=False) + '\n',
                           encoding='utf-8')
    blocked_output = directory / 'package-full.json'
    if blocked_output.exists():
        raise SystemExit(f'refusing to overwrite {blocked_output}')
    blocked = _run_cli(snapshot_path, layout_path, blocked_output)
    if blocked.returncode != 3:
        raise SystemExit(f'expected exit 3, got {blocked.returncode}: '
                         f'{blocked.stderr}')
    forced = json.loads(blocked.stderr)
    if forced != {'forced': expected_forced()}:
        raise SystemExit('forced set mismatch: '
                         f'{json.dumps(forced)[:200]}')
    if blocked_output.exists():
        raise SystemExit('blocked release must not create the package')

    # 第二跑：撤下首门与末门的课程合计，须发布完整包。
    layout_path = directory / 'layout-trimmed.json'
    layout_path.write_text(json.dumps(build_layout(full=False),
                                      ensure_ascii=False) + '\n',
                           encoding='utf-8')
    released_output = directory / 'package-trimmed.json'
    if released_output.exists():
        raise SystemExit(f'refusing to overwrite {released_output}')
    released = _run_cli(snapshot_path, layout_path, released_output)
    if released.returncode != 0:
        raise SystemExit(f'expected exit 0, got {released.returncode}: '
                         f'{released.stderr}')
    verify_package(json.loads(released_output.read_text(encoding='utf-8')))

    print(json.dumps({
        'courses': COURSES, 'days': DAYS, 'protected': COURSES * DAYS,
        'forced_count': len(expected_forced()),
        'forced_first': expected_forced()[0],
        'forced_last': expected_forced()[-1],
        'released_pages': 2,
        'address_space': ADDRESS_SPACE, 'cpu_seconds': CPU_SECONDS,
        'output': str(released_output),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

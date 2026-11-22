"""可复现的盲测编排压测：生成两组合成批次，再计时 CLI 从读入到计划写完。

只用标准库；计时不包含本脚本生成输入的时间。默认在缓存目录写批次与计划，
避免污染仓库（run/ 已在 .gitignore 中）。
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
BUDGET_SECONDS = 20.0


def _summary(fragments):
    return {'bases': 100, 'fragment_bases': 100, 'fragments': fragments,
            'unique_cuts': 0, 'recognition_hits': 0}


def _submission(submission_id, fragments):
    return {'submission_id': submission_id, 'summary': _summary(fragments)}


def identical_groups_batch():
    """32 个互不相连的组，每组 1 single、1 light、1 heavy，目标 (16,16,16)。"""
    submissions = []
    links = []
    for block in range(32):
        ids = [f'g{block:02d}-h', f'g{block:02d}-l', f'g{block:02d}-s']
        submissions.extend([
            _submission(ids[0], 8),
            _submission(ids[1], 2),
            _submission(ids[2], 1),
        ])
        links.extend([[ids[0], ids[1]], [ids[1], ids[2]]])
    return {
        'submissions': submissions,
        'links': links,
        'targets': {'single': 16, 'light': 16, 'heavy': 16},
    }


def geometric_series_batch():
    """三个系列各 9 组，组内链式相连；分档向量按 2 的幂放大，共 27 组。"""
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
                    submission_id = f's{series}g{step}{tier}{copy:04d}'
                    members.append(submission_id)
                    submissions.append(_submission(submission_id, fragments))
            members.sort()
            links.extend([[left, right]
                          for left, right in zip(members, members[1:])])
    return {
        'submissions': submissions,
        'links': links,
        'targets': {'single': 1020, 'light': 1020, 'heavy': 1020},
    }


def run_batch(name, data, directory):
    source = directory / f'{name}.json'
    output = directory / f'{name}-plan.json'
    source.write_text(json.dumps(data), encoding='utf-8')
    # 本次工具自产的旧计划允许重跑：只删本脚本固定命名的计划文件。
    if output.is_file() and not output.is_symlink():
        output.unlink()
    elif output.exists() or output.is_symlink():
        raise SystemExit(f'{name}: refusing to replace non-regular {output}')
    env = dict(os.environ)
    env['PYTHONPATH'] = str(SRC)
    # 计时：从启动 CLI 读入现成批次，到计划文件写完。
    start_ns = time.monotonic_ns()
    result = subprocess.run(
        [sys.executable, '-P', '-m', 'sequence_digest.holdout_cli',
         '--source', str(source), '--output', str(output)],
        cwd=str(ROOT), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    elapsed = (time.monotonic_ns() - start_ns) / 1e9
    if result.returncode != 0:
        raise SystemExit(f'{name}: CLI failed: {result.stderr}')
    plan = json.loads(output.read_text(encoding='utf-8'))
    return elapsed, plan


def main(argv=None):
    parser = argparse.ArgumentParser(description='盲测编排两组压测')
    parser.add_argument('--directory', default=None,
                        help='批次/计划缓存目录（默认使用系统临时目录）')
    args = parser.parse_args(argv)

    if args.directory is None:
        directory = Path(tempfile.mkdtemp(prefix='holdout-bench-'))
    else:
        directory = Path(args.directory)
        directory.mkdir(parents=True, exist_ok=True)

    cases = (
        ('identical-32', identical_groups_batch(), 601080390,
         lambda plan: (all(g['blind_status'] == 'optional' for g in plan['groups']),
                       plan['blind_group_ids'] == [f'g{i:02d}-h' for i in range(16)])),
        ('geometric-27', geometric_series_batch(), 1,
         lambda plan: (sum(1 for g in plan['groups']
                           if g['blind_status'] == 'required') == 24,
                       sum(1 for g in plan['groups']
                           if g['blind_status'] == 'excluded') == 3)),
    )
    failed = False
    for name, data, expected_count, check in cases:
        elapsed, plan = run_batch(name, data, directory)
        passed, extra = check(plan)
        ok = (plan['feasible_count'] == expected_count and passed
              and elapsed < BUDGET_SECONDS)
        failed |= not ok
        print(f'{name}: submissions={len(data["submissions"])} '
              f'groups={len(plan["groups"])} feasible_count={plan["feasible_count"]} '
              f'elapsed={elapsed:.3f}s budget={BUDGET_SECONDS:.0f}s '
              f'status={"PASS" if ok else "FAIL"}')
    if failed:
        return 1
    print(f'plans written under {directory}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""SLA 长跨度夜间对账的可复现验收（验收第 2 条）。

在受限进程**之外**生成 8192 张跨度 104 周的合法 single 工单（两门课各 4096
张，分别采用 12:00 与 12:30 生效的降限版本），再把真实 CLI 放在
``RLIMIT_CPU=20`` 的单进程内从读入跑到输出，最后**逐工单**核对首次违约时刻、
责任队、违约当刻累计与最终累计，并对 12:30 生效版本另跑小批同刻结单用例。

口径换算（全部由脚本断言复核）：

- 开单从 2026-11-09 周一 09:00 提前 104 周到 2024-11-11 周一 09:00，并用 A 的
  日期替换令这一天停服；
- 提前后到原开单时刻多累计 ``104×360−180 = 37260`` 个有效分钟（A 每周服务
  360 分钟，扣除原周一上午的 180 分钟，首个周一整天停服已由替换保证）；
- 旧/新限额分别在 240/180 上加 37260，即 37500/37440；
- 两门课工单分别在 2026-11-10 12:00（累计 37440，记 B）与 12:30（累计 37470，
  记 A）首次违约，最终累计均为 37470。

用法：

    python3 -P tools/sla_bench.py --directory run/sla-bench

只依赖标准库；生成不计入预算，CLI 不委托子进程或外部服务。
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
CPU_SECONDS = 20

EARLY_OPEN = '2024-11-11T09:00Z'
OVERRIDE_DAY = '2024-11-11'
AS_OF = '2026-11-10T13:00Z'
OLD_LIMIT = 37500          # 240 + 37260
NEW_LIMIT = 37440          # 180 + 37260
EXTRA = 104 * 360 - 180    # 37260
FINAL_CHARGED = 37470      # 37260 + 210
SUMMARY = {'bases': 10, 'fragment_bases': 10, 'fragments': 1,
           'unique_cuts': 0, 'recognition_hits': 0}


def _teams():
    empty = [[] for _ in range(7)]
    a = [list(day) for day in empty]
    a[0] = [[540, 720]]            # 周一 09:00-12:00
    a[1] = [[540, 720]]            # 周二 09:00-12:00
    b = [list(day) for day in empty]
    b[1] = [[600, 780]]            # 周二 10:00-13:00
    return [
        {'team_id': 'A', 'weekly': a,
         'overrides': [{'day': OVERRIDE_DAY, 'windows': []}]},
        {'team_id': 'B', 'weekly': b, 'overrides': []},
    ]


def _policies(course_id, new_effective_at):
    limits = {'single': OLD_LIMIT, 'light': OLD_LIMIT, 'heavy': OLD_LIMIT}
    new_limits = dict(limits)
    new_limits['single'] = NEW_LIMIT
    return [
        {'course_id': course_id, 'effective_at': '2024-11-11T00:00Z',
         'limits': limits},
        {'course_id': course_id, 'effective_at': new_effective_at,
         'limits': new_limits},
    ]


def _events(close_instead=False):
    tail = {'at': '2026-11-10T12:30Z',
            'op': 'close' if close_instead else 'transfer',
            **({} if close_instead else {'team_id': 'A'})}
    return [
        {'at': '2026-11-09T10:00Z', 'op': 'pause'},
        {'at': '2026-11-10T10:00Z', 'op': 'resume'},
        {'at': '2026-11-10T11:00Z', 'op': 'transfer', 'team_id': 'B'},
        tail,
    ]


def _ticket(ticket_id, course_id, close_instead=False):
    return {
        'ticket_id': ticket_id, 'course_id': course_id, 'team_id': 'A',
        'opened_at': EARLY_OPEN, 'summary': dict(SUMMARY),
        'events': _events(close_instead),
    }


def build_batch(courses_per=4096):
    """生成验收输入；返回 (输入, 逐工单期望)。``courses_per`` 缩小供单测使用。"""
    assert EXTRA == 37260 and OLD_LIMIT == 240 + EXTRA \
        and NEW_LIMIT == 180 + EXTRA and FINAL_CHARGED == EXTRA + 210
    tickets, expected = [], {}
    for course_id, effective, breach_team, breach_at in (
            ('C1', '2026-11-10T12:00Z', 'B', '2026-11-10T12:00Z'),
            ('C2', '2026-11-10T12:30Z', 'A', '2026-11-10T12:30Z')):
        for index in range(courses_per):
            ticket_id = f'{course_id}-T{index:05d}'
            tickets.append(_ticket(ticket_id, course_id))
            expected[ticket_id] = {
                'breach_at': breach_at, 'team_id': breach_team,
                'breach_charged': NEW_LIMIT if breach_team == 'B'
                else FINAL_CHARGED,
                'final_charged': FINAL_CHARGED, 'state': 'active',
                'effective_at': effective}
    data = {
        'as_of': AS_OF,
        'teams': _teams(),
        'policies': _policies('C1', '2026-11-10T12:00Z')
        + _policies('C2', '2026-11-10T12:30Z'),
        'tickets': tickets,
    }
    return data, expected


def build_close_batch(size=8):
    """12:30 生效版本的小批同刻结单用例：无违约，最终累计仍为 37470。"""
    tickets = [_ticket(f'C3-T{index:03d}', 'C3', close_instead=True)
               for index in range(size)]
    data = {
        'as_of': AS_OF, 'teams': _teams(),
        'policies': _policies('C3', '2026-11-10T12:30Z'),
        'tickets': tickets,
    }
    expected = {ticket['ticket_id']: {
        'breach_at': None, 'final_charged': FINAL_CHARGED,
        'state': 'closed'} for ticket in tickets}
    return data, expected


def verify(result, expected):
    """逐工单核对（不只核对队伍总数），失败时抛 AssertionError 并带工单编号。"""
    rows = {row['ticket_id']: row for row in result['tickets']}
    assert set(rows) == set(expected), 'ticket id set mismatch'
    counts = {}
    for ticket_id, want in expected.items():
        row = rows[ticket_id]
        assert row['tier'] == 'single', ticket_id
        assert row['charged_minutes'] == want['final_charged'], ticket_id
        assert row['state'] == want['state'], ticket_id
        breach = row['first_breach']
        if want['breach_at'] is None:
            assert breach is None, ticket_id
            continue
        assert breach['at'] == want['breach_at'], ticket_id
        assert breach['team_id'] == want['team_id'], ticket_id
        assert breach['charged_minutes'] == want['breach_charged'], ticket_id
        assert breach['limit_minutes'] == NEW_LIMIT, ticket_id
        assert breach['effective_at'] == want['effective_at'], ticket_id
        counts[breach['team_id']] = counts.get(breach['team_id'], 0) + 1
    if expected and next(iter(expected.values()))['breach_at'] is not None:
        per_course = len(expected) // 2
        assert result['breaches_by_team'] == [
            {'team_id': 'A', 'count': per_course},
            {'team_id': 'B', 'count': per_course}], result['breaches_by_team']
        assert sum(counts.values()) == len(expected)


def _limit_cpu():
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))


def _run_cli(source, output):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(SRC)
    return subprocess.run(
        [sys.executable, '-P', '-m', 'sequence_digest.sla_cli',
         '--source', str(source), '--output', str(output)],
        cwd=str(ROOT), env=env, text=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=_limit_cpu)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='生成并在 20 CPU 秒预算内运行第 2 条 SLA 长跨度验收。')
    parser.add_argument('--directory', required=True, help='生成目录')
    args = parser.parse_args(argv)
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)

    data, expected = build_batch()
    source = directory / 'sla-batch.json'
    output = directory / 'sla-digest.json'
    source.write_text(json.dumps(data, ensure_ascii=False) + '\n',
                      encoding='utf-8')
    if output.exists():
        raise SystemExit(f'refusing to overwrite {output}')
    completed = _run_cli(source, output)
    if completed.returncode != 0:
        raise SystemExit(f'CLI failed under RLIMIT_CPU={CPU_SECONDS}: '
                         f'{completed.stderr}')
    verify(json.loads(output.read_text(encoding='utf-8')), expected)

    close_data, close_expected = build_close_batch()
    close_source = directory / 'sla-close.json'
    close_output = directory / 'sla-close-digest.json'
    close_source.write_text(json.dumps(close_data, ensure_ascii=False) + '\n',
                            encoding='utf-8')
    if close_output.exists():
        raise SystemExit(f'refusing to overwrite {close_output}')
    close_run = _run_cli(close_source, close_output)
    if close_run.returncode != 0:
        raise SystemExit(f'close-batch CLI failed: {close_run.stderr}')
    verify(json.loads(close_output.read_text(encoding='utf-8')),
           close_expected)

    print(json.dumps({
        'tickets': len(expected), 'cpu_seconds': CPU_SECONDS,
        'breach_at_noon': {'team': 'B', 'charged': NEW_LIMIT},
        'breach_at_half': {'team': 'A', 'charged': FINAL_CHARGED},
        'final_charged': FINAL_CHARGED,
        'close_batch_no_breach': len(close_expected),
        'output': str(output),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

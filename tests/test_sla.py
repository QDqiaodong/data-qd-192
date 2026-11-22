"""本地复核工单 SLA 核算测试。

覆盖：验收第 1 条三变体、独立逐分钟对照（日期替换优先、暂停中交接、日界线
策略、相邻时段、乱序输入）、边界与非法输入、CLI 不覆盖语义，以及受限 CPU
进程内的长跨度批次。
"""
import datetime
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
from sequence_digest.sla import SlaError, build_digest, format_stamp, stamp

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'

SINGLE_SUMMARY = {'bases': 10, 'fragment_bases': 10, 'fragments': 1,
                  'unique_cuts': 0, 'recognition_hits': 0}


def run_cli(*args, cpu_seconds=None):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(SRC)

    def preexec():
        if cpu_seconds is not None:
            resource.setrlimit(resource.RLIMIT_CPU,
                               (cpu_seconds, cpu_seconds))

    return subprocess.run(
        [sys.executable, '-P', '-m', 'sequence_digest.sla_cli', *args],
        cwd=str(ROOT), env=env, text=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        preexec_fn=preexec if cpu_seconds is not None else None)


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + '\n',
                    encoding='utf-8')
    return path


def scenario_teams():
    empty = [[] for _ in range(7)]
    a = [list(day) for day in empty]
    a[0] = [[540, 720]]            # 周一 09:00-12:00
    a[1] = [[540, 720]]            # 周二 09:00-12:00
    b = [list(day) for day in empty]
    b[1] = [[600, 780]]            # 周二 10:00-13:00
    return [
        {'team_id': 'A', 'weekly': a, 'overrides': []},
        {'team_id': 'B', 'weekly': b, 'overrides': []},
    ]


def scenario_events(last_op):
    return [
        {'at': '2026-11-09T10:00Z', 'op': 'pause'},
        {'at': '2026-11-10T10:00Z', 'op': 'resume'},
        {'at': '2026-11-10T11:00Z', 'op': 'transfer', 'team_id': 'B'},
        {'at': '2026-11-10T12:30Z', 'op': last_op,
         **({'team_id': 'A'} if last_op == 'transfer' else {})},
    ]


def scenario_policies(new_effective_at, old=240, new=180):
    limits = {tier: old for tier in ('single', 'light', 'heavy')}
    new_limits = dict(limits)
    new_limits['single'] = new
    return [
        {'course_id': 'C', 'effective_at': '2026-01-01T00:00Z',
         'limits': limits},
        {'course_id': 'C', 'effective_at': new_effective_at,
         'limits': new_limits},
    ]


def scenario_input(policy_at, last_op, ticket_id='T1'):
    return {
        'as_of': '2026-11-10T13:00Z',
        'teams': scenario_teams(),
        'policies': scenario_policies(policy_at),
        'tickets': [{
            'ticket_id': ticket_id, 'course_id': 'C', 'team_id': 'A',
            'opened_at': '2026-11-09T09:00Z', 'summary': SINGLE_SUMMARY,
            'events': scenario_events(last_op),
        }],
    }


class AcceptanceScenarioTests(unittest.TestCase):
    """验收第 1 条：12:00 降限、12:30 降限、同刻结单三个变体。"""

    def test_breach_at_noon_charged_to_b(self):
        result = build_digest(scenario_input('2026-11-10T12:00Z',
                                             'transfer'))
        row = result['tickets'][0]
        self.assertEqual(row['charged_minutes'], 210)
        self.assertEqual(row['state'], 'active')
        self.assertEqual(row['first_breach'], {
            'at': '2026-11-10T12:00Z', 'team_id': 'B',
            'charged_minutes': 180, 'limit_minutes': 180,
            'effective_at': '2026-11-10T12:00Z'})
        self.assertEqual(result['breaches_by_team'],
                         [{'team_id': 'B', 'count': 1}])

    def test_breach_at_half_past_charged_to_a_after_transfer(self):
        result = build_digest(scenario_input('2026-11-10T12:30Z',
                                             'transfer'))
        row = result['tickets'][0]
        self.assertEqual(row['charged_minutes'], 210)
        self.assertEqual(row['first_breach'], {
            'at': '2026-11-10T12:30Z', 'team_id': 'A',
            'charged_minutes': 210, 'limit_minutes': 180,
            'effective_at': '2026-11-10T12:30Z'})
        self.assertEqual(result['breaches_by_team'],
                         [{'team_id': 'A', 'count': 1}])

    def test_close_at_breach_moment_produces_no_breach(self):
        result = build_digest(scenario_input('2026-11-10T12:30Z', 'close'))
        row = result['tickets'][0]
        self.assertIsNone(row['first_breach'])
        self.assertEqual(row['charged_minutes'], 210)
        self.assertEqual(row['state'], 'closed')
        self.assertEqual(result['breaches_by_team'], [])

    def test_lowering_limit_while_paused_breaches_without_service_minutes(self):
        data = scenario_input('2026-11-09T11:00Z', 'transfer')
        # 暂停中（周一 10:00-周二 10:00）把新限额定到 30：11:00 当刻即违约。
        data['policies'] = scenario_policies('2026-11-09T11:00Z',
                                             old=240, new=30)
        result = build_digest(data)
        breach = result['tickets'][0]['first_breach']
        self.assertEqual(breach['at'], '2026-11-09T11:00Z')
        self.assertEqual(breach['team_id'], 'A')
        self.assertEqual(breach['charged_minutes'], 60)
        self.assertEqual(breach['limit_minutes'], 30)

    def test_as_of_just_under_limit_is_not_a_breach(self):
        data = scenario_input('2026-11-10T12:00Z', 'transfer')
        data['as_of'] = '2026-11-10T11:59Z'
        data['tickets'][0]['events'] = data['tickets'][0]['events'][:3]
        data['policies'] = scenario_policies('2026-11-10T12:00Z',
                                             old=240, new=180)
        result = build_digest(data)
        row = result['tickets'][0]
        self.assertIsNone(row['first_breach'])
        self.assertEqual(row['charged_minutes'], 179)

    def test_raising_limit_after_breach_keeps_original_attribution(self):
        data = scenario_input('2026-11-10T12:00Z', 'transfer')
        data['policies'].append({
            'course_id': 'C', 'effective_at': '2026-11-10T12:30Z',
            'limits': {'single': 1000, 'light': 1000, 'heavy': 1000}})
        result = build_digest(data)
        breach = result['tickets'][0]['first_breach']
        self.assertEqual(breach['at'], '2026-11-10T12:00Z')
        self.assertEqual(breach['team_id'], 'B')
        self.assertEqual(breach['limit_minutes'], 180)
        self.assertEqual(result['tickets'][0]['charged_minutes'], 210)

    def test_breach_strictly_inside_a_quiet_segment(self):
        data = {
            'as_of': '2026-11-09T12:00Z',
            'teams': scenario_teams(),
            'policies': [{
                'course_id': 'C', 'effective_at': '2026-01-01T00:00Z',
                'limits': {'single': 60, 'light': 60, 'heavy': 60}}],
            'tickets': [{
                'ticket_id': 'T1', 'course_id': 'C', 'team_id': 'A',
                'opened_at': '2026-11-09T09:00Z', 'summary': SINGLE_SUMMARY,
                'events': []}],
        }
        row = build_digest(data)['tickets'][0]
        self.assertEqual(row['first_breach']['at'], '2026-11-09T10:00Z')
        self.assertEqual(row['first_breach']['charged_minutes'], 60)
        self.assertEqual(row['charged_minutes'], 180)

    def test_breach_can_land_outside_service_windows_via_limit_cut(self):
        # 服务 09:00-12:00；13:00（已下班、无服务）把限额降到 120，当刻违约。
        data = {
            'as_of': '2026-11-09T13:30Z',
            'teams': scenario_teams(),
            'policies': [
                {'course_id': 'C', 'effective_at': '2026-01-01T00:00Z',
                 'limits': {'single': 240, 'light': 240, 'heavy': 240}},
                {'course_id': 'C', 'effective_at': '2026-11-09T13:00Z',
                 'limits': {'single': 120, 'light': 120, 'heavy': 120}}],
            'tickets': [{
                'ticket_id': 'T1', 'course_id': 'C', 'team_id': 'A',
                'opened_at': '2026-11-09T09:00Z', 'summary': SINGLE_SUMMARY,
                'events': []}],
        }
        row = build_digest(data)['tickets'][0]
        self.assertEqual(row['first_breach']['at'], '2026-11-09T13:00Z')
        self.assertEqual(row['first_breach']['team_id'], 'A')
        self.assertEqual(row['first_breach']['charged_minutes'], 180)
        self.assertEqual(row['first_breach']['limit_minutes'], 120)
        self.assertEqual(oracle(data)['T1'],
                         (row['state'], row['charged_minutes'],
                          row['first_breach']))

    def test_transfer_to_idle_team_attributes_breach_to_new_team(self):
        # 12:00 转给已下班的 A，12:30 降限到 180：责任归 A，尽管 A 不服务。
        data = scenario_input('2026-11-10T12:30Z', 'transfer')
        data['tickets'][0]['events'] = [
            {'at': '2026-11-09T10:00Z', 'op': 'pause'},
            {'at': '2026-11-10T10:00Z', 'op': 'resume'},
            {'at': '2026-11-10T11:00Z', 'op': 'transfer', 'team_id': 'B'},
            {'at': '2026-11-10T12:00Z', 'op': 'transfer', 'team_id': 'A'}]
        result = build_digest(data)
        breach = result['tickets'][0]['first_breach']
        self.assertEqual(breach['at'], '2026-11-10T12:30Z')
        self.assertEqual(breach['team_id'], 'A')
        self.assertEqual(breach['charged_minutes'], 180)
        self.assertEqual(oracle(data)['T1'][2], breach)


# --- 独立逐分钟对照：完全重写一套参考实现 ---------------------------------

EPOCH_ORDINAL = datetime.date(1970, 1, 1).toordinal()


def _ordinal_of(stamp_text):
    return stamp(stamp_text, 'x') // 1440 + EPOCH_ORDINAL


def oracle(data):
    """逐分钟参考实现：不复用 sla.py 的日历/跳跃逻辑，只共享时间解析约定。"""
    as_of = stamp(data['as_of'], 'as_of')
    schedules = {}
    for team in data['teams']:
        weekly = {weekday: sorted(tuple(w) for w in team['weekly'][weekday])
                  for weekday in range(7)}
        overrides = {}
        for item in team['overrides']:
            overrides[_ordinal_of(item['day'] + 'T00:00Z')] = \
                sorted(tuple(w) for w in item['windows'])
        schedules[team['team_id']] = (weekly, overrides)

    def serving(team_id, minute):
        weekly, overrides = schedules[team_id]
        day_ord = minute // 1440 + EPOCH_ORDINAL
        windows = overrides.get(day_ord, weekly[(day_ord - 1) % 7])
        mod = minute % 1440
        return any(start <= mod < end for start, end in windows)

    policies = {}
    for policy in data['policies']:
        policies.setdefault(policy['course_id'], []).append(
            (stamp(policy['effective_at'], 'x'), policy['limits']))
    for entries in policies.values():
        entries.sort()

    rows = {}
    for ticket in data['tickets']:
        course = ticket['course_id']
        entries = policies[course]
        opened = stamp(ticket['opened_at'], 'x')
        tier_index = {1: 0}.get(ticket['summary']['fragments'],
                                1 if ticket['summary']['fragments'] <= 7 else 2)
        policy_pos = 0
        while policy_pos + 1 < len(entries) \
                and entries[policy_pos + 1][0] <= opened:
            policy_pos += 1
        limit = entries[policy_pos][1][
            ('single', 'light', 'heavy')[tier_index]]
        effective = entries[policy_pos][0]
        events = {stamp(e['at'], 'x'): e for e in ticket['events']}
        team_id = ticket['team_id']
        paused = closed = False
        charged = 0
        breach = None
        for moment in range(opened + 1, as_of + 1):
            if not paused and not closed and serving(team_id, moment - 1):
                charged += 1
            event = events.get(moment)
            if event is not None:
                op = event['op']
                if op == 'pause':
                    paused = True
                elif op == 'resume':
                    paused = False
                elif op == 'close':
                    closed = True
                else:
                    team_id = event['team_id']
            while policy_pos + 1 < len(entries) \
                    and entries[policy_pos + 1][0] <= moment:
                policy_pos += 1
                limit = entries[policy_pos][1][
                    ('single', 'light', 'heavy')[tier_index]]
                effective = entries[policy_pos][0]
            if breach is None and not closed and charged >= limit:
                breach = {
                    'at': format_stamp(moment), 'team_id': team_id,
                    'charged_minutes': charged, 'limit_minutes': limit,
                    'effective_at': format_stamp(effective)}
        state = 'closed' if closed else ('paused' if paused else 'active')
        rows[ticket['ticket_id']] = (state, charged, breach)
    return rows


def _random_input(rng):
    """生成保证合法、覆盖各种边界的小跨度输入。"""
    base = datetime.date(2026, 1, 5)          # 周一
    days = rng.randint(2, 5)
    team_ids = ['A', 'B']
    teams = []
    for team_id in team_ids:
        weekly = []
        for _ in range(7):
            windows = []
            cursor = rng.randint(0, 600)
            for _ in range(rng.randint(0, 2)):
                length = rng.randint(30, 180)
                end = min(cursor + length, 1440)
                if cursor < end:
                    windows.append([cursor, end])
                cursor = end + rng.choice((0, 30, 120))  # 0 => 相邻时段
            weekly.append(windows)
        overrides = []
        if rng.random() < 0.5:
            day = base + datetime.timedelta(days=rng.randrange(days))
            overrides.append({
                'day': day.isoformat(),
                'windows': [] if rng.random() < 0.4
                else [[rng.randint(0, 700), rng.randint(720, 1440)]]})
        teams.append({'team_id': team_id, 'weekly': weekly,
                      'overrides': overrides})

    course_id = 'C'
    opened_day = base
    opened = f'{opened_day.isoformat()}T0{rng.randint(0, 8)}:00Z'
    opened_min = stamp(opened, 'x')
    policies = [{
        'course_id': course_id,
        'effective_at': f'{(opened_day - datetime.timedelta(days=1)).isoformat()}T00:00Z',
        'limits': {'single': rng.randint(1, 400), 'light': 999,
                   'heavy': 999}}]
    moment = opened_min
    for _ in range(rng.randint(0, 3)):
        moment += rng.randint(60, 1440)
        if moment >= opened_min + days * 1440:
            break
        policies.append({
            'course_id': course_id, 'effective_at': format_stamp(moment),
            'limits': {'single': rng.randint(1, 400), 'light': 999,
                       'heavy': 999}})

    events = []
    moment = opened_min
    state = 'active'
    team_id = 'A'
    closed = False
    as_of_min = opened_min + days * 1440 - rng.randint(0, 60)
    while moment < as_of_min and len(events) < 10 and not closed:
        moment += rng.randint(30, 360)
        if moment >= as_of_min:
            break
        choices = ['transfer', 'pause'] if state == 'active' \
            else ['resume', 'transfer']
        op = rng.choice(choices + (['close'] if rng.random() < 0.2 else []))
        event = {'at': format_stamp(moment), 'op': op}
        if op == 'transfer':
            team_id = 'B' if team_id == 'A' else 'A'
            event['team_id'] = team_id
        elif op == 'pause':
            state = 'paused'
        elif op == 'resume':
            state = 'active'
        else:
            closed = True
        events.append(event)

    tickets = [{
        'ticket_id': 'T1', 'course_id': course_id,
        'team_id': 'A', 'opened_at': opened,
        'summary': dict(SINGLE_SUMMARY), 'events': events}]
    return {'as_of': format_stamp(as_of_min), 'teams': teams,
            'policies': policies, 'tickets': tickets}


class OracleCrossCheckTests(unittest.TestCase):
    def test_random_tickets_match_minute_by_minute_oracle(self):
        rng = random.Random(20261122)
        checked = 0
        for _ in range(80):
            data = _random_input(rng)
            expected = oracle(data)
            result = build_digest(data)
            for row in result['tickets']:
                state, charged, breach = expected[row['ticket_id']]
                self.assertEqual(row['state'], state, data)
                self.assertEqual(row['charged_minutes'], charged, data)
                self.assertEqual(row['first_breach'], breach, data)
            checked += 1
        self.assertGreaterEqual(checked, 70)

    def test_override_takes_priority_over_weekly(self):
        data = _random_input(random.Random(1))
        # 确定性用例：周一替换为停服，当天一分不计。
        teams = scenario_teams()
        teams[0]['overrides'] = [{'day': '2026-11-09', 'windows': []}]
        data = {
            'as_of': '2026-11-09T12:00Z', 'teams': teams,
            'policies': [{'course_id': 'C',
                          'effective_at': '2026-01-01T00:00Z',
                          'limits': {'single': 240, 'light': 240,
                                     'heavy': 240}}],
            'tickets': [{'ticket_id': 'T1', 'course_id': 'C',
                         'team_id': 'A', 'opened_at': '2026-11-09T09:00Z',
                         'summary': SINGLE_SUMMARY, 'events': []}]}
        row = build_digest(data)['tickets'][0]
        self.assertEqual(row['charged_minutes'], 0)
        self.assertEqual(oracle(data)['T1'], ('active', 0, None))

    def test_transfer_while_paused_keeps_paused_state(self):
        events = [
            {'at': '2026-11-09T10:00Z', 'op': 'pause'},
            {'at': '2026-11-09T11:00Z', 'op': 'transfer', 'team_id': 'B'},
            {'at': '2026-11-10T10:00Z', 'op': 'resume'},
        ]
        data = {
            'as_of': '2026-11-10T13:00Z', 'teams': scenario_teams(),
            'policies': [{'course_id': 'C',
                          'effective_at': '2026-01-01T00:00Z',
                          'limits': {'single': 240, 'light': 240,
                                     'heavy': 240}}],
            'tickets': [{'ticket_id': 'T1', 'course_id': 'C',
                         'team_id': 'A', 'opened_at': '2026-11-09T09:00Z',
                         'summary': SINGLE_SUMMARY, 'events': events}]}
        row = build_digest(data)['tickets'][0]
        # 周一 60 分钟（A），暂停中交接不计时；周二 10:00-13:00 由 B 计 180。
        self.assertEqual(row['charged_minutes'], 240)
        self.assertEqual(oracle(data)['T1'],
                         (row['state'], row['charged_minutes'],
                          row['first_breach']))

    def test_policy_effective_at_midnight_day_boundary(self):
        # 旧限额 240：周一服务 180 分钟，00:00 降限到 5 当刻累计 180 即违约。
        data = {
            'as_of': '2026-11-10T09:30Z', 'teams': scenario_teams(),
            'policies': [
                {'course_id': 'C', 'effective_at': '2026-01-01T00:00Z',
                 'limits': {'single': 240, 'light': 240, 'heavy': 240}},
                {'course_id': 'C', 'effective_at': '2026-11-10T00:00Z',
                 'limits': {'single': 5, 'light': 5, 'heavy': 5}}],
            'tickets': [{'ticket_id': 'T1', 'course_id': 'C',
                         'team_id': 'A', 'opened_at': '2026-11-09T09:00Z',
                         'summary': SINGLE_SUMMARY, 'events': []}]}
        row = build_digest(data)['tickets'][0]
        self.assertEqual(row['first_breach']['at'],
                         '2026-11-10T00:00Z')
        self.assertEqual(row['first_breach']['limit_minutes'], 5)
        self.assertEqual(oracle(data)['T1'],
                         (row['state'], row['charged_minutes'],
                          row['first_breach']))

    def test_adjacent_windows_count_as_continuous_service(self):
        teams = [{
            'team_id': 'A',
            'weekly': [[[540, 630], [630, 720]]]
                      + [[] for _ in range(6)],
            'overrides': []}]
        data = {
            'as_of': '2026-11-09T12:00Z', 'teams': teams,
            'policies': [{'course_id': 'C',
                          'effective_at': '2026-01-01T00:00Z',
                          'limits': {'single': 240, 'light': 240,
                                     'heavy': 240}}],
            'tickets': [{'ticket_id': 'T1', 'course_id': 'C',
                         'team_id': 'A', 'opened_at': '2026-11-09T09:00Z',
                         'summary': SINGLE_SUMMARY, 'events': []}]}
        row = build_digest(data)['tickets'][0]
        self.assertEqual(row['charged_minutes'], 180)
        self.assertEqual(oracle(data)['T1'],
                         (row['state'], row['charged_minutes'],
                          row['first_breach']))


class ValidationTests(unittest.TestCase):
    def base(self):
        return scenario_input('2026-11-10T12:00Z', 'transfer')

    def test_structural_rejections(self):
        bad = self.base()
        bad['extra'] = 1
        with self.assertRaises(SlaError):
            build_digest(bad)
        with self.assertRaises(SlaError):
            build_digest({'as_of': '2026-11-10T13:00Z',
                          'teams': [], 'policies': [], 'tickets': []})
        with self.assertRaises(SlaError):
            build_digest({'as_of': '2026-11-10T13:00Z',
                          'teams': self.base()['teams'],
                          'policies': [], 'tickets': []})

    def test_bad_timestamps_and_dates(self):
        for value in ('2026-11-09 09:00Z', '2026-11-09T24:00Z',
                      '2026-11-09T9:00Z', '2026-11-09T09:00+01:00',
                      '2026-02-29T00:00Z', '2026-13-01T00:00Z',
                      '2026-11-09T09:60Z'):
            bad = self.base()
            bad['as_of'] = value
            with self.assertRaises(SlaError):
                build_digest(bad)
        bad = self.base()
        bad['teams'][0]['overrides'] = [{'day': '2026-02-30', 'windows': []}]
        with self.assertRaises(SlaError):
            build_digest(bad)

    def test_window_rules(self):
        bad = self.base()
        bad['teams'][0]['weekly'][0] = [[540, 720], [600, 660]]  # 重叠
        with self.assertRaises(SlaError):
            build_digest(bad)
        bad = self.base()
        bad['teams'][0]['weekly'][0] = [[720, 540]]              # start>=end
        with self.assertRaises(SlaError):
            build_digest(bad)
        bad = self.base()
        bad['teams'][0]['weekly'][0] = [[0, 1441]]               # 越界
        with self.assertRaises(SlaError):
            build_digest(bad)
        bad = self.base()
        bad['teams'][0]['weekly'] = [[] for _ in range(6)]      # 少于 7 天
        with self.assertRaises(SlaError):
            build_digest(bad)

    def test_duplicate_identities(self):
        bad = self.base()
        bad['teams'][1]['team_id'] = 'A'
        with self.assertRaises(SlaError):
            build_digest(bad)
        bad = self.base()
        bad['teams'][0]['overrides'] = [
            {'day': '2026-11-09', 'windows': []},
            {'day': '2026-11-09', 'windows': [[0, 10]]}]
        with self.assertRaises(SlaError):
            build_digest(bad)
        bad = self.base()
        bad['policies'][0]['effective_at'] = bad['policies'][1]['effective_at']
        with self.assertRaises(SlaError):
            build_digest(bad)
        bad = self.base()
        bad['tickets'].append(dict(bad['tickets'][0]))
        with self.assertRaises(SlaError):
            build_digest(bad)

    def test_policy_must_exist_at_open_but_future_policies_are_ok(self):
        bad = self.base()
        bad['policies'][0]['effective_at'] = '2026-11-09T09:01Z'
        with self.assertRaises(SlaError):
            build_digest(bad)
        good = self.base()
        good['policies'].append({
            'course_id': 'C', 'effective_at': '2027-01-01T00:00Z',
            'limits': {'single': 1, 'light': 1, 'heavy': 1}})
        build_digest(good)

    def test_event_state_machine_and_fields(self):
        cases = [
            ([{'at': '2026-11-09T09:00Z', 'op': 'pause'}], 'at open'),
            ([{'at': '2026-11-10T14:00Z', 'op': 'pause'}], 'after as_of'),
            ([{'at': '2026-11-09T10:00Z', 'op': 'pause'},
              {'at': '2026-11-09T11:00Z', 'op': 'pause'}], 'double pause'),
            ([{'at': '2026-11-09T10:00Z', 'op': 'resume'}], 'resume active'),
            ([{'at': '2026-11-09T10:00Z', 'op': 'transfer',
               'team_id': 'A'}], 'same team'),
            ([{'at': '2026-11-09T10:00Z', 'op': 'transfer',
               'team_id': 'Z'}], 'unknown team'),
            ([{'at': '2026-11-09T10:00Z', 'op': 'frobnicate'}], 'bad op'),
            ([{'at': '2026-11-09T10:00Z', 'op': 'pause',
               'team_id': 'B'}], 'extra field'),
            ([{'at': '2026-11-09T10:00Z', 'op': 'transfer'}], 'missing team'),
        ]
        for events, label in cases:
            bad = self.base()
            bad['tickets'][0]['events'] = events
            with self.assertRaises(SlaError, msg=label):
                build_digest(bad)
        bad = self.base()
        bad['tickets'][0]['events'] = [
            {'at': '2026-11-09T10:00Z', 'op': 'pause'},
            {'at': '2026-11-09T10:00Z', 'op': 'resume'}]
        with self.assertRaises(SlaError):
            build_digest(bad)
        bad = self.base()
        bad['tickets'][0]['events'] = [
            {'at': '2026-11-09T10:00Z', 'op': 'close'},
            {'at': '2026-11-09T11:00Z', 'op': 'pause'}]
        with self.assertRaises(SlaError):
            build_digest(bad)

    def test_booleans_are_not_integers_and_limits_are_bounded(self):
        bad = self.base()
        bad['policies'][0]['limits']['single'] = True
        with self.assertRaises(SlaError):
            build_digest(bad)
        bad = self.base()
        bad['teams'][0]['weekly'][0] = [[True, 720]]
        with self.assertRaises(SlaError):
            build_digest(bad)
        bad = self.base()
        bad['policies'][0]['limits']['single'] = 1051201
        with self.assertRaises(SlaError):
            build_digest(bad)

    def test_summary_contract_is_reused(self):
        bad = self.base()
        bad['tickets'][0]['summary'] = dict(SINGLE_SUMMARY, fragments=0)
        with self.assertRaises(SlaError):
            build_digest(bad)
        good_light = self.base()
        good_light['tickets'][0]['summary'] = dict(SINGLE_SUMMARY, fragments=4)
        self.assertEqual(build_digest(good_light)['tickets'][0]['tier'],
                         'light')
        good_heavy = self.base()
        good_heavy['tickets'][0]['summary'] = dict(SINGLE_SUMMARY, fragments=8)
        self.assertEqual(build_digest(good_heavy)['tickets'][0]['tier'],
                         'heavy')

    def test_span_boundary_and_open_equals_as_of(self):
        early_limits = {'single': 1, 'light': 1, 'heavy': 1}
        good = self.base()
        good['policies'].insert(0, {
            'course_id': 'C', 'effective_at': '2024-01-01T00:00Z',
            'limits': early_limits})
        good['tickets'][0]['opened_at'] = '2024-11-10T13:00Z'
        good['tickets'][0]['events'] = []
        self.assertEqual(stamp('2026-11-10T13:00Z', 'x')
                         - stamp('2024-11-10T13:00Z', 'x'), 730 * 1440)
        build_digest(good)                    # 恰好 730 天：合法
        bad = self.base()
        bad['policies'].insert(0, {
            'course_id': 'C', 'effective_at': '2024-01-01T00:00Z',
            'limits': early_limits})
        bad['tickets'][0]['opened_at'] = '2024-11-10T12:59Z'
        bad['tickets'][0]['events'] = []
        self.assertEqual(stamp('2026-11-10T13:00Z', 'x')
                         - stamp('2024-11-10T12:59Z', 'x'),
                         730 * 1440 + 1)
        with self.assertRaises(SlaError):
            build_digest(bad)                # 超过 730 天（730 天零 1 分钟）
        edge = {
            'as_of': '2026-11-09T09:00Z', 'teams': scenario_teams(),
            'policies': [{'course_id': 'C',
                          'effective_at': '2026-01-01T00:00Z',
                          'limits': {'single': 1, 'light': 1, 'heavy': 1}}],
            'tickets': [{'ticket_id': 'T1', 'course_id': 'C',
                         'team_id': 'A', 'opened_at': '2026-11-09T09:00Z',
                         'summary': SINGLE_SUMMARY, 'events': []}]}
        row = build_digest(edge)['tickets'][0]
        self.assertEqual((row['charged_minutes'], row['state'],
                          row['first_breach']), (0, 'active', None))

    def test_empty_ticket_list_is_valid(self):
        data = self.base()
        data['tickets'] = []
        result = build_digest(data)
        self.assertEqual(result['tickets'], [])
        self.assertEqual(result['breaches_by_team'], [])


class DeterminismTests(unittest.TestCase):
    def test_reordering_input_keeps_output_bytes(self):
        rng = random.Random(99)
        data = _random_input(rng)
        data['tickets'].append({
            'ticket_id': 'T2', 'course_id': 'C', 'team_id': 'B',
            'opened_at': data['tickets'][0]['opened_at'],
            'summary': dict(SINGLE_SUMMARY, fragments=9),
            'events': []})
        baseline = json.dumps(build_digest(data), sort_keys=True,
                              ensure_ascii=False)
        for _ in range(5):
            shuffled = json.loads(json.dumps(data))
            rng.shuffle(shuffled['teams'])
            rng.shuffle(shuffled['policies'])
            rng.shuffle(shuffled['tickets'])
            for ticket in shuffled['tickets']:
                rng.shuffle(ticket['events'])
            again = json.dumps(build_digest(shuffled), sort_keys=True,
                               ensure_ascii=False)
            self.assertEqual(again, baseline)


class CliTests(unittest.TestCase):
    def test_cli_roundtrip_and_stdout_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = write_json(Path(tmp) / 'in.json',
                                scenario_input('2026-11-10T12:00Z',
                                               'transfer'))
            output = Path(tmp) / 'out.json'
            result = run_cli('--source', str(source), '--output', str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(payload['schema_version'], 1)
            self.assertEqual(payload['tickets'][0]['first_breach']['team_id'],
                             'B')
            self.assertIn('"tickets":1', result.stdout.replace(' ', ''))

    def test_never_overwrites_existing_target_or_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = write_json(Path(tmp) / 'in.json',
                                scenario_input('2026-11-10T12:00Z',
                                               'transfer'))
            output = Path(tmp) / 'out.json'
            first = run_cli('--source', str(source), '--output', str(output))
            self.assertEqual(first.returncode, 0)
            original = output.read_bytes()
            second = run_cli('--source', str(source), '--output', str(output))
            self.assertEqual(second.returncode, 2)
            self.assertEqual(output.read_bytes(), original)

            target = Path(tmp) / 'real.json'
            target.write_text('keep\n', encoding='utf-8')
            link = Path(tmp) / 'link.json'
            link.symlink_to(target)
            third = run_cli('--source', str(source), '--output', str(link))
            self.assertEqual(third.returncode, 2)
            self.assertEqual(target.read_text(encoding='utf-8'), 'keep\n')

    def test_bad_input_leaves_no_partial_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = write_json(Path(tmp) / 'bad.json', {'as_of': 'nope'})
            output = Path(tmp) / 'out.json'
            result = run_cli('--source', str(bad), '--output', str(output))
            self.assertEqual(result.returncode, 2)
            self.assertNotEqual(result.stderr, '')
            self.assertEqual(result.stdout, '')
            self.assertFalse(output.exists())

    def test_duplicate_json_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'dup.json'
            source.write_text(
                '{"as_of":"2026-11-10T13:00Z","as_of":"2026-11-10T14:00Z",'
                '"teams":[],"policies":[],"tickets":[]}', encoding='utf-8')
            result = run_cli('--source', str(source),
                             '--output', str(Path(tmp) / 'out.json'))
            self.assertEqual(result.returncode, 2)
            self.assertFalse((Path(tmp) / 'out.json').exists())

    def test_nonfinite_and_oversize_files_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            nan_file = Path(tmp) / 'nan.json'
            nan_file.write_text(
                '{"as_of":"2026-11-10T13:00Z","teams":[],"policies":[],"tickets":[]}',
                encoding='utf-8')
            big = Path(tmp) / 'big.json'
            with big.open('w', encoding='utf-8') as handle:
                handle.write('{"as_of":"2026-11-10T13:00Z","teams":[],'
                             '"policies":[],"tickets":[')
                handle.write('x' * 8000001)
                handle.write(']}')
            result = run_cli('--source', str(big),
                             '--output', str(Path(tmp) / 'out.json'))
            self.assertEqual(result.returncode, 2)
            self.assertFalse((Path(tmp) / 'out.json').exists())

    def test_long_span_batch_runs_under_cpu_limit(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'sla_bench', ROOT / 'tools' / 'sla_bench.py')
        bench = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bench)
        with tempfile.TemporaryDirectory() as tmp:
            source, expected = bench.build_batch(courses_per=64)
            source_path = write_json(Path(tmp) / 'batch.json', source)
            self.assertLess(source_path.stat().st_size, 8_000_000)
            output = Path(tmp) / 'out.json'
            result = run_cli('--source', str(source_path),
                             '--output', str(output), cpu_seconds=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            bench.verify(json.loads(output.read_text(encoding='utf-8')),
                         expected)


if __name__ == '__main__':
    unittest.main()

"""本地复核工单 SLA 核算：判断每张工单是否首次违约、在哪一分钟违约、归哪支队。

输入只有教学合成数字摘要、日期/时刻与业务编号，不读取作业内容或切分规则，也不
改动任何既有命令；运行只依赖 Python 标准库。

核算口径
--------
- 从开单起累积**有效分钟**：某分钟只有在其开始时工单正在办理（非暂停、未结单），
  且落在当时负责队的服务时段（左闭右开）内才计费，在下一整分钟时刻计入累计。
- 每个整分钟时刻先计入刚结束的一分钟，再应用该时刻的交接/暂停/恢复/结单事件与
  生效策略，最后才判断违约；同刻结单不产生新违约。
- 策略采用不晚于当前时刻的最近一条；新策略也适用于未结工单，不重置累计。
- 首次违约一旦发生，不因结单、调高时限或再交接而撤销或改名。

实现按工单的「关键时刻」做**区间跳跃**而不是逐分钟空转（见 :func:`simulate_ticket`
与 :func:`service_minutes`），保证 730 天跨度、8192 张工单仍在固定 CPU 预算内。
"""
import datetime
import re
from bisect import bisect_left, bisect_right

from .checks import array, fields, ident, integer
from .holdout import TIERS, validate_summary

SCHEMA_VERSION = 1
MAX_TEAMS = 256
MAX_OVERRIDES = 128
MAX_WINDOWS = 8
MAX_POLICIES = 8192
MAX_TICKETS = 8192
MAX_EVENTS = 64
MAX_LIMIT = 1051200
MAX_SPAN_MINUTES = 730 * 1440
TOP_FIELDS = ('as_of', 'teams', 'policies', 'tickets')
TEAM_FIELDS = ('team_id', 'weekly', 'overrides')
OVERRIDE_FIELDS = ('day', 'windows')
POLICY_FIELDS = ('course_id', 'effective_at', 'limits')
TICKET_FIELDS = ('ticket_id', 'course_id', 'team_id', 'opened_at',
                 'summary', 'events')
EVENT_FIELDS = ('at', 'op')
_STAMP_PATTERN = re.compile(
    r'[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])'
    r'T(?:[01][0-9]|2[0-3]):[0-5][0-9]Z')
_DAY_PATTERN = re.compile(
    r'[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])')
EPOCH_ORDINAL = datetime.date(1970, 1, 1).toordinal()


class SlaError(ValueError):
    """SLA 核算的业务/结构错误；CLI 统一以退出码 2 报告。"""


# --- 基础校验 -------------------------------------------------------------

def _ident(value):
    try:
        return ident(value)
    except ValueError as exc:
        raise SlaError('invalid id') from exc


def _fields(obj, names, what):
    try:
        return fields(obj, names)
    except ValueError as exc:
        raise SlaError(f'{what} must contain exactly {", ".join(names)}') from exc


def _int(value, low, high, what):
    try:
        return integer(value, low, high)
    except ValueError as exc:
        raise SlaError(f'{what} must be an integer {low}..{high}') from exc


def _array(value, low, high, what):
    try:
        return array(value, low, high)
    except ValueError as exc:
        raise SlaError(f'{what} must be an array of size {low}..{high}') from exc


def stamp(value, what):
    """严格 ``YYYY-MM-DDTHH:MMZ``（UTC 整分钟），返回自纪元起的整数分钟。"""
    if not isinstance(value, str) or _STAMP_PATTERN.fullmatch(value) is None:
        raise SlaError(f'{what} must be a strict YYYY-MM-DDTHH:MMZ timestamp')
    try:
        day = datetime.date(int(value[:4]), int(value[5:7]), int(value[8:10]))
    except ValueError as exc:
        raise SlaError(f'{what} has an invalid calendar date: {value}') from exc
    return (day.toordinal() - EPOCH_ORDINAL) * 1440 \
        + int(value[11:13]) * 60 + int(value[14:16])


def _day(value):
    """合法公历日期，严格 ``YYYY-MM-DD``；返回其公历序号。"""
    if not isinstance(value, str) or _DAY_PATTERN.fullmatch(value) is None:
        raise SlaError('day must be a strict YYYY-MM-DD date')
    try:
        day = datetime.date(int(value[:4]), int(value[5:7]), int(value[8:10]))
    except ValueError as exc:
        raise SlaError(f'invalid calendar day: {value}') from exc
    return day.toordinal()


def format_stamp(minute):
    """整数分钟转严格 UTC 时刻字符串，与 :func:`stamp` 互逆。"""
    day = datetime.date.fromordinal(EPOCH_ORDINAL + minute // 1440)
    minute_of_day = minute % 1440
    return f'{day.isoformat()}T{minute_of_day // 60:02d}:{minute_of_day % 60:02d}Z'


def _windows(value, what):
    """单日时段数组：0–8 个不重叠、不跨日的 [start,end] 整数分钟时段。"""
    rows = _array(value, 0, MAX_WINDOWS, what)
    ordered = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            raise SlaError(f'{what} windows must be [start,end] minute pairs')
        start = _int(row[0], 0, 1440, f'{what} window start')
        end = _int(row[1], 0, 1440, f'{what} window end')
        if start >= end:
            raise SlaError(f'{what} window must satisfy start < end')
        ordered.append((start, end))
    ordered.sort()
    for index in range(1, len(ordered)):
        # 相邻允许（端点相等），重叠不允许；时段天然不跨日。
        if ordered[index][0] < ordered[index - 1][1]:
            raise SlaError(f'{what} windows must not overlap')
    return tuple(ordered)


# --- 整份输入校验 ----------------------------------------------------------

def validate_input(data):
    """校验整份输入（含未来策略与首次违约之后的事件），返回规范化结构。"""
    _fields(data, TOP_FIELDS, 'input')
    as_of = stamp(data['as_of'], 'as_of')

    team_ids, weekly, overrides = _validate_teams(data['teams'])
    policies_by_course = _validate_policies(data['policies'])
    tickets = _validate_tickets(
        data['tickets'], as_of, set(team_ids), policies_by_course)
    return {
        'as_of': as_of,
        'team_ids': team_ids,
        'weekly': weekly,
        'overrides': overrides,
        'policies': policies_by_course,
        'tickets': tickets,
    }


def _validate_teams(value):
    rows = _array(value, 1, MAX_TEAMS, 'teams')
    team_ids, weekly, overrides = [], {}, {}
    for row in rows:
        _fields(row, TEAM_FIELDS, 'team')
        team_id = _ident(row['team_id'])
        if team_id in weekly:
            raise SlaError(f'duplicate team_id: {team_id}')
        schedule = _array(row['weekly'], 7, 7, f'team {team_id} weekly')
        team_weekly = tuple(
            _windows(schedule[weekday], f'team {team_id} weekly[{weekday}]')
            for weekday in range(7))
        team_overrides = {}
        for item in _array(row['overrides'], 0, MAX_OVERRIDES,
                          f'team {team_id} overrides'):
            _fields(item, OVERRIDE_FIELDS, 'team override')
            day = _day(item['day'])
            if day in team_overrides:
                raise SlaError(
                    f'duplicate override day for team {team_id}: {item["day"]}')
            team_overrides[day] = _windows(
                item['windows'], f'team {team_id} override {item["day"]}')
        team_ids.append(team_id)
        weekly[team_id] = team_weekly
        overrides[team_id] = team_overrides
    return team_ids, weekly, overrides


def _validate_policies(value):
    rows = _array(value, 1, MAX_POLICIES, 'policies')
    by_course = {}
    seen = set()
    for row in rows:
        _fields(row, POLICY_FIELDS, 'policy')
        course_id = _ident(row['course_id'])
        effective = stamp(row['effective_at'], 'policy effective_at')
        if (course_id, effective) in seen:
            raise SlaError(
                f'duplicate policy: {course_id} at {row["effective_at"]}')
        seen.add((course_id, effective))
        _fields(row['limits'], TIERS, 'policy limits')
        limits = tuple(
            _int(row['limits'][tier], 1, MAX_LIMIT, f'limits.{tier}')
            for tier in TIERS)
        by_course.setdefault(course_id, []).append((effective, limits))
    for entries in by_course.values():
        entries.sort()
    return by_course


def _validate_tickets(value, as_of, team_ids, policies_by_course):
    rows = _array(value, 0, MAX_TICKETS, 'tickets')
    tickets = []
    seen = set()
    for row in rows:
        _fields(row, TICKET_FIELDS, 'ticket')
        ticket_id = _ident(row['ticket_id'])
        if ticket_id in seen:
            raise SlaError(f'duplicate ticket_id: {ticket_id}')
        seen.add(ticket_id)
        course_id = _ident(row['course_id'])
        team_id = _ident(row['team_id'])
        if team_id not in team_ids:
            raise SlaError(f'ticket {ticket_id} team_id is not registered: '
                           f'{team_id}')
        opened = stamp(row['opened_at'], 'opened_at')
        if opened > as_of:
            raise SlaError(f'ticket {ticket_id} opened after as_of')
        if as_of - opened > MAX_SPAN_MINUTES:
            raise SlaError(f'ticket {ticket_id} spans more than 730 days')
        try:
            tier_index = validate_summary(row['summary'])
        except ValueError as exc:
            raise SlaError(f'ticket {ticket_id} summary: {exc}') from exc
        entries = policies_by_course.get(course_id)
        if not entries or entries[0][0] > opened:
            raise SlaError(
                f'ticket {ticket_id}: course {course_id} has no policy '
                f'effective at or before opening')
        events = _validate_events(
            row['events'], ticket_id, opened, as_of, team_id, team_ids)
        tickets.append({
            'ticket_id': ticket_id,
            'course_id': course_id,
            'team_id': team_id,
            'opened_at': opened,
            'tier_index': tier_index,
            'events': events,
        })
    return tickets


def _validate_events(value, ticket_id, opened, as_of, initial_team, team_ids):
    """事件允许乱序输入：先做逐条结构/时刻/编号校验并按时刻排序，再按时间顺序
    复核状态机（开单即办理中；办理中才可暂停、暂停中才可恢复；两种状态都可
    交接或结单；交接不改暂停状态；结单后禁止再有事件）。
    """
    rows = _array(value, 0, MAX_EVENTS, f'ticket {ticket_id} events')
    parsed = []
    seen_moments = set()
    for row in rows:
        try:
            fields(row, EVENT_FIELDS, ('team_id',))
        except ValueError as exc:
            raise SlaError(
                f'ticket {ticket_id}: event must contain at/op and optional team_id'
            ) from exc
        op = row['op']
        if op not in ('pause', 'resume', 'close', 'transfer'):
            raise SlaError(f'ticket {ticket_id}: unknown event op: {op}')
        if op == 'transfer':
            if set(row) != set(EVENT_FIELDS + ('team_id',)):
                raise SlaError(
                    f'ticket {ticket_id}: transfer must carry only team_id')
            next_team = _ident(row['team_id'])
            if next_team not in team_ids:
                raise SlaError(
                    f'ticket {ticket_id}: transfer team is not registered: '
                    f'{next_team}')
        elif set(row) != set(EVENT_FIELDS):
            raise SlaError(f'ticket {ticket_id}: {op} carries extra fields')
        moment = stamp(row['at'], 'event at')
        if not opened < moment <= as_of:
            raise SlaError(
                f'ticket {ticket_id}: event at must be strictly after opening '
                f'and no later than as_of')
        if moment in seen_moments:
            raise SlaError(f'ticket {ticket_id}: duplicate event at {row["at"]}')
        seen_moments.add(moment)
        parsed.append((moment, op)
                      + ((row['team_id'],) if op == 'transfer' else ()))
    parsed.sort(key=lambda event: event[0])

    state = 'active'          # 开单即办理中
    team_id = initial_team
    closed = False
    for moment, op, *rest in parsed:
        if closed:
            raise SlaError(f'ticket {ticket_id}: no events allowed after close')
        if op == 'pause':
            if state != 'active':
                raise SlaError(f'ticket {ticket_id}: pause requires active state')
            state = 'paused'
        elif op == 'resume':
            if state != 'paused':
                raise SlaError(f'ticket {ticket_id}: resume requires paused state')
            state = 'active'
        elif op == 'close':
            closed = True
        else:  # transfer：两种状态都可交接，不改变暂停状态
            next_team = rest[0]
            if next_team == team_id:
                raise SlaError(
                    f'ticket {ticket_id}: transfer must change the team')
            team_id = next_team
    return parsed


# --- 服务日历 --------------------------------------------------------------

def _day_windows(team_id, day_index, weekly, overrides):
    """某天的有效时段：日期替换整体优先于周模板；空数组表示当天不上班。"""
    absolute = overrides.get(day_index)
    if absolute is not None:
        return absolute
    return weekly[(day_index - 1) % 7]   # date ordinal 1 是周一


def build_calendars(validated, first_day, last_day):
    """为每支队预建覆盖 ``[first_day, last_day]``（公历序号）的日历时点与前缀
    有效分钟。

    每天存有序时段与当天总有效分钟；``prefix[k+1]`` 是从第 0 天到第 k 天
    （含）的有效分钟累计，使任意区间的有效分钟可在对数时间求出。
    """
    span = last_day - first_day + 1
    calendars = {}
    for team_id in validated['team_ids']:
        days = [None] * span
        prefix = [0] * (span + 1)
        weekly = validated['weekly'][team_id]
        overrides = validated['overrides'][team_id]
        running = 0
        for offset in range(span):
            day_index = first_day + offset
            windows = _day_windows(team_id, day_index, weekly, overrides)
            total = sum(end - start for start, end in windows)
            days[offset] = (windows, total)
            running += total
            prefix[offset + 1] = running
        calendars[team_id] = {'first_day': first_day, 'days': days,
                              'prefix': prefix}
    return calendars


def _day_offsets(calendar, start, end):
    """把两个整数分钟时刻换算成日历数组内的天偏移。"""
    first_day = calendar['first_day']
    return (start // 1440 + EPOCH_ORDINAL - first_day,
            end // 1440 + EPOCH_ORDINAL - first_day)


def service_minutes(calendar, start, end):
    """半开区间 ``[start, end)`` 内（整数分钟时刻）落在服务时段的分钟数。"""
    if end <= start:
        return 0
    days = calendar['days']
    prefix = calendar['prefix']
    start_index, end_index = _day_offsets(calendar, start, end)
    if start_index == end_index:
        windows, _ = days[start_index]
        return _window_minutes(windows, start % 1440, end % 1440)
    start_windows, _ = days[start_index]
    end_windows, _ = days[end_index]
    first_part = _window_minutes(start_windows, start % 1440, 1440)
    last_part = _window_minutes(end_windows, 0, end % 1440)
    middle = prefix[end_index] - prefix[start_index + 1]
    return first_part + middle + last_part


def _window_minutes(windows, start, end):
    """同一天内半开分钟区间 ``[start, end)`` 与各时段（左闭右开）的交长。"""
    if end <= start:
        return 0
    total = 0
    for window_start, window_end in windows:
        overlap_start = max(start, window_start)
        overlap_end = min(end, window_end)
        if overlap_end > overlap_start:
            total += overlap_end - overlap_start
    return total


def kth_service_tick(calendar, start, ordinal):
    """从时刻 ``start`` 起第 ``ordinal`` 个有效分钟结束的整分钟时刻。

    ``ordinal >= 1``；调用方保证该有效分钟存在。先查开始当天的剩余时段，再用
    前缀和二分定位到后续哪一天，最后在当天至多 8 个时段内顺序定位；长跨度也
    不会逐分钟扫描（零服务日由前缀的等值平台自然跳过）。
    """
    first_day = calendar['first_day']
    days = calendar['days']
    prefix = calendar['prefix']
    start_index, _ = _day_offsets(calendar, start, start)
    start_windows, _ = days[start_index]
    start_mod = start % 1440
    within_day = 0
    for window_start, window_end in start_windows:
        if window_end <= start_mod:
            continue
        piece = window_end - max(start_mod, window_start)
        if ordinal <= within_day + piece:
            # 第 ordinal 个有效分钟在 window_start+(ordinal-1) 开始，其结束
            # （计入累计、可能触发违约）的整分钟时刻要再 +1。
            minute_of_day = max(start_mod, window_start) + ordinal - within_day
            return (first_day + start_index - EPOCH_ORDINAL) * 1440 + minute_of_day
        within_day += piece
    remaining = ordinal - within_day
    base = prefix[start_index + 1]
    day_index = bisect_left(prefix, base + remaining) - 1
    windows, _ = days[day_index]
    remaining -= prefix[day_index] - base
    for window_start, window_end in windows:
        length = window_end - window_start
        if remaining <= length:
            minute_of_day = window_start + remaining
            return (first_day + day_index - EPOCH_ORDINAL) * 1440 + minute_of_day
        remaining -= length
    raise SlaError('internal error: service minute not found')


# --- 工单模拟 --------------------------------------------------------------

def simulate_ticket(ticket, validated, calendars):
    """按关键时刻跳跃模拟一张工单，返回 (最终状态, 累计分钟, 首次违约或 None)。

    循环不变量：``cursor`` 到下一个关键时刻之间，负责队、暂停状态、适用时限都
    不变，因此这段时间的有效分钟可以整段求和；若累计会在段内达到时限，则用
    :func:`kth_service_tick` 直接定位违约分钟，绝不逐分钟空转。每个关键时刻先
    补计刚结束的分钟，再按「事件 → 生效策略 → 违约判断」的顺序处理。

    关键时刻由两条已排序流（事件、同课程策略生效时刻）与 ``as_of`` 归并产生，
    用双指针线性推进，不为每张工单重建集合或排序；同刻的事件与策略在同一轮
    内全部应用后再判违约。

    段内达到时限的那一分钟若恰好在边界时刻结束，违约必须**延后**到该时刻的
    事件与策略应用之后再判断（责任取处理后的当前队、时限取新生效值），因此
    段内只记录严格早于边界时刻的违约，重合情形交给当刻判断。
    """
    opened = ticket['opened_at']
    as_of = validated['as_of']
    policies = validated['policies'][ticket['course_id']]
    effective_moments = [item[0] for item in policies]
    policy_index = bisect_right(effective_moments, opened) - 1
    limits = policies[policy_index][1]
    limit = limits[ticket['tier_index']]
    policy_effective = policies[policy_index][0]

    events = ticket['events']
    event_count = len(events)
    event_index = 0
    # 只需考虑 (opened, as_of] 内的策略生效时刻；两端都用二分夹住。
    policy_cursor = policy_index + 1
    policy_limit = bisect_right(effective_moments, as_of)

    team_id = ticket['team_id']
    paused = False
    closed = False
    charged = 0
    breach = None
    cursor = opened
    while True:
        next_event = events[event_index][0] if event_index < event_count else None
        next_policy = effective_moments[policy_cursor] \
            if policy_cursor < policy_limit else None
        candidates = [value for value in (next_event, next_policy, as_of)
                      if value is not None]
        moment = min(candidates)
        if not closed and not paused:
            added = service_minutes(calendars[team_id], cursor, moment)
            if added:
                if breach is None and charged + added >= limit:
                    breach_tick = kth_service_tick(
                        calendars[team_id], cursor, limit - charged)
                    if breach_tick < moment:
                        # 段内无事件/策略变化，队与时限就是当前值。
                        breach = {
                            'at': format_stamp(breach_tick),
                            'team_id': team_id,
                            'charged_minutes': limit,
                            'limit_minutes': limit,
                            'effective_at': format_stamp(policy_effective),
                        }
                charged += added
        cursor = moment
        while event_index < event_count and events[event_index][0] == moment:
            event = events[event_index]
            op = event[1]
            if op == 'pause':
                paused = True
            elif op == 'resume':
                paused = False
            elif op == 'close':
                closed = True
            else:
                team_id = event[2]
            event_index += 1
        while policy_cursor < policy_limit \
                and effective_moments[policy_cursor] == moment:
            policy_index = policy_cursor
            limits = policies[policy_index][1]
            limit = limits[ticket['tier_index']]
            policy_effective = policies[policy_index][0]
            policy_cursor += 1
        if breach is None and not closed and charged >= limit:
            breach = {
                'at': format_stamp(moment),
                'team_id': team_id,
                'charged_minutes': charged,
                'limit_minutes': limit,
                'effective_at': format_stamp(policy_effective),
            }
        if moment == as_of:
            break
    state = 'closed' if closed else ('paused' if paused else 'active')
    return state, charged, breach


def build_digest(data):
    """校验整份输入并核算全部工单，返回可直接发布的结果字典。

    输出只依赖输入内容：所有列表按业务编号排序，与输入数组顺序无关，合法输入
    重排产生相同字节（发布时统一 ``sort_keys``）。
    """
    validated = validate_input(data)
    tickets = validated['tickets']
    if tickets:
        first_day = min(ticket['opened_at'] for ticket in tickets) // 1440 \
            + EPOCH_ORDINAL
        last_day = validated['as_of'] // 1440 + EPOCH_ORDINAL
        calendars = build_calendars(validated, first_day, last_day)
    else:
        calendars = {}

    rows = []
    breach_counts = {}
    for ticket in tickets:
        state, charged, breach = simulate_ticket(ticket, validated, calendars)
        rows.append({
            'ticket_id': ticket['ticket_id'],
            'tier': TIERS[ticket['tier_index']],
            'charged_minutes': charged,
            'state': state,
            'first_breach': breach,
        })
        if breach is not None:
            team_id = breach['team_id']
            breach_counts[team_id] = breach_counts.get(team_id, 0) + 1
    rows.sort(key=lambda row: row['ticket_id'])
    breaches_by_team = [
        {'team_id': team_id, 'count': breach_counts[team_id]}
        for team_id in sorted(breach_counts)
    ]
    return {
        'schema_version': SCHEMA_VERSION,
        'as_of': data['as_of'],
        'tickets': rows,
        'breaches_by_team': breaches_by_team,
    }

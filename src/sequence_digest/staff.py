"""复核产能排班：把台账快照里的复核点需求排给助教队的产能班次。

输入只有教学合成数字、日期与业务编号：现有台账的 ``snapshot`` JSON 与一份
排班花名册（班次与旧草案预约）。模块不打开完整切分报告，不读源字符串、片段
内容或切分规则，也不改变任何识别/切分能力；运行只依赖 Python 标准库。

排的是**整数点产能**，不是逐份作业派单：一个班次代表一支助教队在某一天可用的
点数，可以拆分给不同课程。

契约要点
--------
- 所有已确认（``locked_points``）必须原班次、原课程、原结算日保留，并全额
  占用原班次容量；确认超过该桶最新需求的部分只记为该桶的确认超额，不能挪给
  别的桶，也不能省出容量。
- 对剩余需求安排可改点数，不得造成超额，不得超容量或违反资格/日期约束。
- 第一目标：最大化实际覆盖的需求点数；第二目标：在覆盖不降的前提下，最大化
  留在原配对上、不超过原 ``points - locked_points`` 的可改点数。
- 同分最优解可以不同，但同一实现对同值输入（无论输入数组怎样重排）产生相同
  字节。

求解把排班写成一次最小费用最大流：班次残量 → 桶剩余需求是容量边，原配对上
「保留额度」的那部分边费用为 -1，其余为 0。最小费用最大流先保证流量最大
（第一目标），再让 -1 边的流量最大（第二目标）。
"""
import datetime
import heapq
import re
from bisect import bisect_right

from .checks import array, fields, ident, integer

SCHEMA_VERSION = 1
MAX_BUCKETS = 8192
MAX_BUCKET_POINTS = 1000000
MAX_SHIFTS = 2000
MAX_COURSES_PER_SHIFT = 256
MAX_CAPACITY = 1000000
MAX_PAIRS = 12000
MAX_BOOKINGS = 12000
MAX_BOOKING_POINTS = 1000000
MAX_PLAN_DAYS = 31
TIERS = ('single', 'light', 'heavy')
TIER_POINTS = (1, 3, 6)
SNAPSHOT_FIELDS = ('schema_version', 'head', 'buckets')
BUCKET_FIELDS = ('course_id', 'day', 'counts', 'points')
ROSTER_FIELDS = ('start_day', 'end_day', 'shifts', 'bookings')
SHIFT_FIELDS = ('shift_id', 'day', 'courses', 'capacity')
BOOKING_FIELDS = ('shift_id', 'course_id', 'day', 'points', 'locked_points')
_DAY_PATTERN = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


class StaffError(ValueError):
    """排班的业务/结构错误；CLI 统一以退出码 2 报告。"""


# --- 输入校验 -------------------------------------------------------------

def _fields(obj, names, what):
    try:
        return fields(obj, names)
    except ValueError as exc:
        raise StaffError(f'{what} must contain exactly {", ".join(names)}') from exc


def _array(value, low, high, what):
    try:
        return array(value, low, high)
    except ValueError as exc:
        raise StaffError(f'{what} must be an array of size {low}..{high}') from exc


def _ident(value):
    try:
        return ident(value)
    except ValueError as exc:
        raise StaffError('invalid id') from exc


def _int(value, low, high, what):
    try:
        return integer(value, low, high)
    except ValueError as exc:
        raise StaffError(f'{what} must be an integer {low}..{high}') from exc


def _day(value):
    """合法公历日期，严格 ``YYYY-MM-DD``（四位年、两位月日）。"""
    if not isinstance(value, str) or _DAY_PATTERN.fullmatch(value) is None:
        raise StaffError('day must be a strict YYYY-MM-DD date')
    try:
        datetime.date(int(value[:4]), int(value[5:7]), int(value[8:10]))
    except ValueError as exc:
        raise StaffError(f'invalid calendar day: {value}') from exc
    return value


def _span_days(start, end):
    return (datetime.date(int(end[:4]), int(end[5:7]), int(end[8:10]))
            - datetime.date(int(start[:4]), int(start[5:7]),
                            int(start[8:10]))).days + 1


def _counts(value):
    try:
        fields(value, TIERS)
    except ValueError as exc:
        raise StaffError('bucket counts must contain exactly single/light/heavy') from exc
    result = []
    for tier in TIERS:
        result.append(_int(value[tier], 0, MAX_BUCKET_POINTS,
                           f'counts.{tier}'))
    return tuple(result)


def validate_snapshot(data):
    """校验 ``sequence-digest-ledger snapshot`` 的 stdout JSON。

    返回 ``{'head', 'buckets'}``，桶为 ``(course_id, day) -> {...}``。
    """
    _fields(data, SNAPSHOT_FIELDS, 'snapshot')
    if type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise StaffError('snapshot must be schema_version 1')
    head = data['head']
    if type(head) is not int or head < 0:
        raise StaffError('snapshot head must be a non-negative integer')
    items = _array(data['buckets'], 0, MAX_BUCKETS, 'snapshot buckets')
    buckets = {}
    for item in items:
        _fields(item, BUCKET_FIELDS, 'snapshot bucket')
        course_id = _ident(item['course_id'])
        day = _day(item['day'])
        key = (course_id, day)
        if key in buckets:
            raise StaffError(f'duplicate snapshot bucket: {course_id}/{day}')
        counts = _counts(item['counts'])
        points = _int(item['points'], 0, MAX_BUCKET_POINTS, 'bucket points')
        expected = sum(counts[index] * TIER_POINTS[index]
                       for index in range(3))
        if points != expected:
            raise StaffError(f'bucket {course_id}/{day} points do not match counts')
        if points == 0:
            raise StaffError(f'bucket {course_id}/{day} must not be all zero')
        buckets[key] = {'counts': counts, 'points': points, 'day': day}
    return {'head': head, 'buckets': buckets}


def validate_roster(data):
    """校验计划期、班次与旧草案预约；锁定点必须始终合法且不超容量。"""
    _fields(data, ROSTER_FIELDS, 'roster')
    start = _day(data['start_day'])
    end = _day(data['end_day'])
    if start > end:
        raise StaffError('start_day must not be after end_day')
    if _span_days(start, end) > MAX_PLAN_DAYS:
        raise StaffError(f'plan period must be at most {MAX_PLAN_DAYS} days')

    shifts = {}
    for item in _array(data['shifts'], 0, MAX_SHIFTS, 'roster shifts'):
        _fields(item, SHIFT_FIELDS, 'shift')
        shift_id = _ident(item['shift_id'])
        if shift_id in shifts:
            raise StaffError(f'duplicate shift_id: {shift_id}')
        day = _day(item['day'])
        if not start <= day <= end:
            raise StaffError(f'shift {shift_id} day is outside the plan period')
        courses = []
        seen = set()
        for value in _array(item['courses'], 0, MAX_COURSES_PER_SHIFT,
                            'shift courses'):
            course_id = _ident(value)
            if course_id in seen:
                raise StaffError(f'duplicate course in shift {shift_id}')
            seen.add(course_id)
            courses.append(course_id)
        courses.sort()
        capacity = _int(item['capacity'], 0, MAX_CAPACITY, 'shift capacity')
        shifts[shift_id] = {'shift_id': shift_id, 'day': day,
                            'courses': frozenset(seen),
                            'course_list': tuple(courses),
                            'capacity': capacity}

    bookings = []
    seen_pairs = set()
    for item in _array(data['bookings'], 0, MAX_BOOKINGS, 'roster bookings'):
        _fields(item, BOOKING_FIELDS, 'booking')
        shift_id = _ident(item['shift_id'])
        course_id = _ident(item['course_id'])
        day = _day(item['day'])
        if not start <= day <= end:
            raise StaffError('booking day is outside the plan period')
        points = _int(item['points'], 1, MAX_BOOKING_POINTS, 'booking points')
        locked = _int(item['locked_points'], 0, points,
                      'booking locked_points')
        key = (shift_id, course_id, day)
        if key in seen_pairs:
            raise StaffError('duplicate booking for shift/course/day')
        seen_pairs.add(key)
        bookings.append({'shift_id': shift_id, 'course_id': course_id,
                         'day': day, 'points': points,
                         'locked_points': locked})

    # 正数确认必须匹配仍存在、合格且日期合规的班次。
    locked_by_shift = {}
    for booking in bookings:
        locked = booking['locked_points']
        if not locked:
            continue
        shift = shifts.get(booking['shift_id'])
        if shift is None:
            raise StaffError('a confirmed booking references a withdrawn shift')
        if booking['course_id'] not in shift['courses']:
            raise StaffError('a confirmed booking references an ineligible shift')
        if shift['day'] > booking['day']:
            raise StaffError('a confirmed booking has a shift after its settlement day')
        locked_by_shift[booking['shift_id']] = \
            locked_by_shift.get(booking['shift_id'], 0) + locked
    for shift_id, total in locked_by_shift.items():
        if total > shifts[shift_id]['capacity']:
            raise StaffError(
                f'confirmed points on shift {shift_id} exceed its capacity')

    return {'start': start, 'end': end, 'shifts': shifts,
            'bookings': bookings, 'locked_by_shift': locked_by_shift}


def _eligible_pair_count(snapshot, roster):
    """计划期内正需求桶与合格且日期合规班次的配对数，超过上限即拒绝。"""
    course_days = {}
    for shift in roster['shifts'].values():
        for course_id in shift['course_list']:
            course_days.setdefault(course_id, []).append(shift['day'])
    for days in course_days.values():
        days.sort()
    total = 0
    for (course_id, day), bucket in snapshot['buckets'].items():
        if not roster['start'] <= day <= roster['end']:
            continue
        if bucket['points'] <= 0:
            continue
        days = course_days.get(course_id)
        if days:
            total += bisect_right(days, day)
            if total > MAX_PAIRS:
                raise StaffError(
                    f'eligible shift/bucket pairs exceed {MAX_PAIRS}')
    return total


# --- 最小费用最大流 -------------------------------------------------------

class _Flow:
    """紧凑的残量网络；边按加入顺序编号，``e ^ 1`` 是反向边。"""

    __slots__ = ('size', 'adj', 'head', 'capacity', 'cost')

    def __init__(self, size):
        self.size = size
        self.adj = [[] for _ in range(size)]
        self.head = []
        self.capacity = []
        self.cost = []

    def add(self, tail, head, capacity, cost):
        index = len(self.head)
        self.adj[tail].append(index)
        self.head.append(head)
        self.capacity.append(capacity)
        self.cost.append(cost)
        self.adj[head].append(index + 1)
        self.head.append(tail)
        self.capacity.append(0)
        self.cost.append(-cost)
        return index

    def initial_potential(self):
        """初始可行势：源→班次→桶→汇按层序一趟松弛（反向边容量为 0）。"""
        potential = [0] * self.size
        for tail in range(self.size):
            base = potential[tail]
            for edge in self.adj[tail]:
                if self.capacity[edge] <= 0:
                    continue
                candidate = base + self.cost[edge]
                head = self.head[edge]
                if candidate < potential[head]:
                    potential[head] = candidate
        return potential

    def _dijkstra(self, source, sink, potential):
        infinity = float('inf')
        distance = [infinity] * self.size
        distance[source] = 0
        heap = [(0, source)]
        while heap:
            current, tail = heapq.heappop(heap)
            if current != distance[tail]:
                continue
            for edge in self.adj[tail]:
                if self.capacity[edge] <= 0:
                    continue
                head = self.head[edge]
                step = self.cost[edge] + potential[tail] - potential[head]
                candidate = current + step
                if candidate < distance[head]:
                    distance[head] = candidate
                    heapq.heappush(heap, (candidate, head))
        return distance

    def _blocking(self, source, sink, potential):
        """在零约简费用边上推完一个阻塞流。"""
        level = [-1] * self.size
        level[source] = 0
        queue = [source]
        for tail in queue:
            for edge in self.adj[tail]:
                if self.capacity[edge] <= 0:
                    continue
                head = self.head[edge]
                if level[head] < 0 \
                        and self.cost[edge] + potential[tail] - potential[head] == 0:
                    level[head] = level[tail] + 1
                    queue.append(head)
        if level[sink] < 0:
            return 0
        cursor = [0] * self.size
        path = []
        tail = source
        pushed = 0
        while True:
            if tail == sink:
                amount = min(self.capacity[edge] for edge in path)
                for edge in path:
                    self.capacity[edge] -= amount
                    self.capacity[edge ^ 1] += amount
                pushed += amount
                cut = 0
                while self.capacity[path[cut]] > 0:
                    cut += 1
                path = path[:cut]
                tail = source if not path else self.head[path[-1]]
                continue
            advanced = False
            adjacency = self.adj[tail]
            while cursor[tail] < len(adjacency):
                edge = adjacency[cursor[tail]]
                head = self.head[edge]
                if self.capacity[edge] > 0 and level[head] == level[tail] + 1 \
                        and self.cost[edge] + potential[tail] - potential[head] == 0:
                    path.append(edge)
                    tail = head
                    advanced = True
                    break
                cursor[tail] += 1
            if advanced:
                continue
            level[tail] = -1
            if not path:
                break
            edge = path.pop()
            tail = self.head[edge ^ 1]
            cursor[tail] += 1
        return pushed

    def max_flow_min_cost(self, source, sink):
        potential = self.initial_potential()
        infinity = float('inf')
        total = 0
        while True:
            distance = self._dijkstra(source, sink, potential)
            if distance[sink] == infinity:
                break
            for node in range(self.size):
                if distance[node] != infinity:
                    potential[node] += distance[node]
            pushed = self._blocking(source, sink, potential)
            if pushed == 0:
                break
            total += pushed
        return total

# --- 排班 -----------------------------------------------------------------

def build_plan(snapshot_data, roster_data):
    """校验快照与花名册，返回可直接发布的排班计划字典。

    第一目标（最大覆盖）与第二目标（最大保留）由最小费用最大流一次求出；
    之后按原预约与流结果重建分配行。输入数组顺序不影响输出字节。
    """
    snapshot = validate_snapshot(snapshot_data)
    roster = validate_roster(roster_data)
    _eligible_pair_count(snapshot, roster)
    start = roster['start']
    end = roster['end']
    shifts = roster['shifts']
    buckets = snapshot['buckets']
    bookings = roster['bookings']

    # 计划期外的合法桶不参与排班；只保留仍有正需求的桶。
    demand = {}
    for key, bucket in buckets.items():
        if not start <= bucket['day'] <= end or bucket['points'] <= 0:
            continue
        demand[key] = bucket['points']
    ordered_buckets = sorted(demand)

    # 正数确认按 (课程, 结算日) 聚合：先在桶内抵扣最新需求，超出部分为该桶的
    # 确认超额；桶已从最新快照消失的确认同样保留，但只作为超额登记。
    locked_by_bucket = {}
    for booking in bookings:
        locked = booking['locked_points']
        if not locked:
            continue
        key = (booking['course_id'], booking['day'])
        locked_by_bucket[key] = locked_by_bucket.get(key, 0) + locked

    # 可改点数的候选对：班次容量扣掉全部确认后的残量，桶只留剩余需求。
    shift_residual = {}
    for shift_id, shift in shifts.items():
        shift_residual[shift_id] = \
            shift['capacity'] - roster['locked_by_shift'].get(shift_id, 0)
    covered_from_locked = {}
    locked_surplus = {}
    residual_demand = {}
    for key in ordered_buckets:
        covered = min(locked_by_bucket.get(key, 0), demand[key])
        covered_from_locked[key] = covered
        locked_surplus[key] = locked_by_bucket.get(key, 0) - covered
        residual_demand[key] = demand[key] - covered

    # 原配对：同一 (班次, 课程, 结算日) 的未确认上限，独立于班次是否有残量。
    original_pairs = {}
    for booking in bookings:
        bound = booking['points'] - booking['locked_points']
        if bound <= 0:
            continue
        key = (booking['shift_id'], booking['course_id'], booking['day'])
        original_pairs[key] = max(original_pairs.get(key, 0), bound)

    # 资格对：班次课程在名单内，且结算日不早于班次日期。
    eligible = {}
    for shift_id, shift in shifts.items():
        if shift_residual[shift_id] <= 0:
            continue
        day = shift['day']
        for key in ordered_buckets:
            if key[0] in shift['courses'] and key[1] >= day:
                eligible.setdefault(key, []).append(shift_id)

    new_points = _solve(shift_residual, residual_demand, eligible,
                        original_pairs)

    # 分配行：原预约的确认 + 新排可改点；没有原预约的新配对也单列一行。
    provisional_by_pair = {}
    for pair, amount in new_points.items():
        if amount > 0:
            provisional_by_pair[pair] = amount
    assignments = []
    for booking in bookings:
        pair = (booking['shift_id'], booking['course_id'], booking['day'])
        amount = provisional_by_pair.pop(pair, 0)
        total = booking['locked_points'] + amount
        if total > 0:
            assignments.append(_assignment(pair, total, booking['locked_points']))
    for pair in sorted(provisional_by_pair):
        amount = provisional_by_pair[pair]
        if amount > 0:
            assignments.append(_assignment(pair, amount, 0))
    assignments.sort(key=lambda row: (row['shift_id'], row['course_id'],
                                      row['day']))

    provisional_by_bucket = {}
    for (shift_id, course_id, day), amount in new_points.items():
        if amount > 0:
            key = (course_id, day)
            provisional_by_bucket[key] = \
                provisional_by_bucket.get(key, 0) + amount

    bucket_rows = []
    total_covered = 0
    for key in ordered_buckets:
        provisional = provisional_by_bucket.get(key, 0)
        covered = min(demand[key], covered_from_locked[key] + provisional)
        total_covered += covered
        bucket_rows.append({
            'course_id': key[0], 'day': key[1], 'demand_points': demand[key],
            'covered_points': covered,
            'shortage_points': demand[key] - covered,
            'locked_surplus_points': locked_surplus[key],
        })
    # 已无需求但仍有确认的桶：全部为确认超额，也要列出。
    for key in sorted(locked_by_bucket):
        if key in demand:
            continue
        if not start <= key[1] <= end:
            continue
        bucket_rows.append({
            'course_id': key[0], 'day': key[1], 'demand_points': 0,
            'covered_points': 0, 'shortage_points': 0,
            'locked_surplus_points': locked_by_bucket[key],
        })
    bucket_rows.sort(key=lambda row: (row['course_id'], row['day']))

    retained = 0
    for pair, amount in new_points.items():
        if amount <= 0:
            continue
        bound = original_pairs.get(pair, 0)
        if bound > 0:
            retained += min(amount, bound)

    return {
        'schema_version': SCHEMA_VERSION,
        'head': snapshot['head'],
        'assignments': assignments,
        'buckets': bucket_rows,
        'covered_points': total_covered,
        'retained_provisional_points': retained,
    }


def _assignment(pair, points, locked_points):
    return {'shift_id': pair[0], 'course_id': pair[1], 'day': pair[2],
            'points': points, 'locked_points': locked_points}


def _solve(shift_residual, residual_demand, eligible, original_pairs):
    """建图求最小费用最大流，返回可改点数的 ``(shift, course, day) -> 点数``。

    每个 (班次, 桶) 至多两条并行边：容量为保留额度、费用 -1 的一条，以及其余
    容量的费用 0 边。最小费用最大流先最大流量（最大覆盖），再用 -1 边最大化
    保留点数；保留额度内的可改点都算保留，超出才不保留。
    """
    buckets = [key for key in sorted(residual_demand)
               if residual_demand[key] > 0]
    if not buckets:
        return {}
    usable = {}
    for key in buckets:
        for shift_id in sorted(eligible.get(key, ())):
            if shift_residual[shift_id] > 0:
                usable.setdefault(key, []).append(shift_id)
    if not usable:
        return {}
    active_shifts = sorted({shift_id for keys in usable.values()
                            for shift_id in keys})

    source = 0
    shift_nodes = {shift_id: 1 + index
                   for index, shift_id in enumerate(active_shifts)}
    base = 1 + len(active_shifts)
    bucket_nodes = {key: base + index for index, key in enumerate(buckets)}
    sink = base + len(buckets)
    flow = _Flow(sink + 1)
    for shift_id in active_shifts:
        flow.add(source, shift_nodes[shift_id], shift_residual[shift_id], 0)
    pair_edges = {}
    for key in buckets:
        bucket_node = bucket_nodes[key]
        for shift_id in usable.get(key, ()):
            shift_node = shift_nodes[shift_id]
            cap = min(shift_residual[shift_id], residual_demand[key])
            bound = min(original_pairs.get((shift_id, key[0], key[1]), 0), cap)
            edges = []
            if bound > 0:
                edges.append((flow.add(shift_node, bucket_node, bound, -1),
                              bound))
            if cap > bound:
                edges.append(
                    (flow.add(shift_node, bucket_node, cap - bound, 0),
                     cap - bound))
            pair_edges[(shift_id, key)] = edges
        flow.add(bucket_node, sink, residual_demand[key], 0)

    flow.max_flow_min_cost(source, sink)

    result = {}
    for (shift_id, key), edges in pair_edges.items():
        used = sum(initial - flow.capacity[edge] for edge, initial in edges)
        if used > 0:
            result[(shift_id, key[0], key[1])] = used
    return result

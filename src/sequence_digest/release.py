"""周报联发审查：整套附件联合刊登前，本地核对受保护格是否会被反推。

课程组的公开周报由多份附件组成，同一批收件人会把所有附件放在一起看：有的
附件只给课程合计，有的给结算日合计或少量单格。本模块用台账 ``snapshot`` 的
JSON 与一份版面（哪些课程、哪些结算日、哪个档位、保护哪些格子、每份附件刊登
哪些单格与合计）判断：仅凭**整套附件的刊登数值**，收件人能否反推出任一受
保护格的精确计数。只处理教学合成计数，不读作业内容，不读台账内部文件，也
不改变任何既有命令；运行只依赖 Python 标准库。

推断口径
--------
收件人知道课程×结算日矩形与每格的公开取值范围
``0..floor(1000000/档位权重)``（权重 single/light/heavy 依次为 1/3/6），以及
全部附件刊登的单格值与合计值；重复刊登不产生新约束。不假定收件人掌握台账、
缺桶列表、其他档位、旧期数字或外部事实。一张不同的整数表只要在公开范围内、
逐条满足这些单格值与合计值，就是可能的原表。每个受保护格须各自至少有两个
可能值（见证不必出自同一张替代表）；否则该格被**固定**，整套不得发出。

怎样判定整数格可变
------------------
以真实表为基点，受保护格 ±1 的可行性等价于残量图上的可达性：

- 每个未刊登格 ``x<上限`` 时有一条前向边（课程→日期），``x>0`` 时有一条
  后向边（日期→课程）；已刊登格被收件人固定，两边都没有。
- 未公布合计的行/列是自由节点：净额任意，两两之间可自由中转（合计未公开
  意味着它既能吸收也能提供任意差额）。
- 给受保护格 ``(a,b)`` 加一，当且仅当 ``x<上限`` 且存在一条从 b 回到 a 的
  补偿路径（把行/列合计补平）；减一对称。简单路径不会中途重新经过起点，
  所以只需在起点第一步排除该格自身的反向残量边。
- 行、列合计的约束矩阵是二部图关联矩阵（全幺模），整数表与实数表的可行
  投影一致：实数表上可行的调整必有同范围的整数调整，按整数值逐步移动
  不会把实数变化误当整数变化，也不会漏掉已固定的格。

两方向都不可动（或该格本身已被刊登）即判定为可推出。
"""
import datetime
import re

from .checks import array, fields, ident
from .staff import validate_snapshot

SCHEMA_VERSION = 1
MAX_COURSES = 256
MAX_DAYS = 32
MAX_PAGES = 8
MAX_POINTS = 1000000
TIERS = ('single', 'light', 'heavy')
TIER_WEIGHTS = (1, 3, 6)
LAYOUT_FIELDS = ('courses', 'days', 'tier', 'protected', 'pages')
PAGE_FIELDS = ('page_id', 'cells', 'course_totals', 'day_totals')
_DAY_PATTERN = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


class ReleaseError(ValueError):
    """版面/快照/发布的业务或结构错误；CLI 统一以退出码 2 报告。"""


# --- 输入校验 -------------------------------------------------------------

def _fields(obj, names, what):
    try:
        return fields(obj, names)
    except ValueError as exc:
        raise ReleaseError(
            f'{what} must contain exactly {", ".join(names)}') from exc


def _array(value, low, high, what):
    try:
        return array(value, low, high)
    except ValueError as exc:
        raise ReleaseError(
            f'{what} must be an array of size {low}..{high}') from exc


def _ident(value, what):
    try:
        return ident(value)
    except ValueError as exc:
        raise ReleaseError(f'{what} must be an ASCII id') from exc


def _day(value):
    """合法公历日期，严格 ``YYYY-MM-DD``（四位年、两位月日）。"""
    if not isinstance(value, str) or _DAY_PATTERN.fullmatch(value) is None:
        raise ReleaseError('day must be a strict YYYY-MM-DD date')
    try:
        datetime.date(int(value[:4]), int(value[5:7]), int(value[8:10]))
    except ValueError as exc:
        raise ReleaseError(f'invalid calendar day: {value}') from exc
    return value


def _cell_ref(value, course_index, day_index, what):
    """``[course_id, day]`` 格子引用，必须落在矩形内；返回下标对。"""
    if not isinstance(value, list) or len(value) != 2:
        raise ReleaseError(f'{what} must be a [course_id, day] pair')
    course_id, day = value
    if not isinstance(course_id, str) or course_id not in course_index:
        raise ReleaseError(f'{what} references a course outside the rectangle')
    if not isinstance(day, str) or day not in day_index:
        raise ReleaseError(f'{what} references a day outside the rectangle')
    return (course_index[course_id], day_index[day])


def validate_layout(data):
    """校验版面，返回规范化的课程/日期/保护清单/附件结构。

    课程与日期在内部按身份升序排列，之后所有下标即身份顺序；输入列表怎样
    重排都不影响结果字节。
    """
    _fields(data, LAYOUT_FIELDS, 'layout')
    courses = []
    seen = set()
    for value in _array(data['courses'], 1, MAX_COURSES, 'layout courses'):
        course_id = _ident(value, 'layout course')
        if course_id in seen:
            raise ReleaseError(f'duplicate course: {course_id}')
        seen.add(course_id)
        courses.append(course_id)
    days = []
    seen = set()
    for value in _array(data['days'], 1, MAX_DAYS, 'layout days'):
        day = _day(value)
        if day in seen:
            raise ReleaseError(f'duplicate day: {day}')
        seen.add(day)
        days.append(day)
    tier = data['tier']
    if tier not in TIERS:
        raise ReleaseError('tier must be single, light or heavy')
    courses.sort()
    days.sort()
    course_index = {course_id: index for index, course_id in enumerate(courses)}
    day_index = {day: index for index, day in enumerate(days)}
    cells_bound = len(courses) * len(days)

    protected = []
    seen = set()
    for value in _array(data['protected'], 1, cells_bound, 'layout protected'):
        cell = _cell_ref(value, course_index, day_index, 'protected cell')
        if cell in seen:
            raise ReleaseError('duplicate protected cell')
        seen.add(cell)
        protected.append(cell)

    pages = []
    seen_page_ids = set()
    for page in _array(data['pages'], 2, MAX_PAGES, 'layout pages'):
        _fields(page, PAGE_FIELDS, 'page')
        page_id = _ident(page['page_id'], 'page_id')
        if page_id in seen_page_ids:
            raise ReleaseError(f'duplicate page_id: {page_id}')
        seen_page_ids.add(page_id)
        cells = []
        seen = set()
        for value in _array(page['cells'], 0, cells_bound,
                            f'page {page_id} cells'):
            cell = _cell_ref(value, course_index, day_index,
                             f'page {page_id} cell')
            if cell in seen:
                raise ReleaseError(f'duplicate cell in page {page_id}')
            seen.add(cell)
            cells.append(cell)
        course_totals = []
        seen = set()
        for value in _array(page['course_totals'], 0, len(courses),
                            f'page {page_id} course_totals'):
            if not isinstance(value, str) or value not in course_index:
                raise ReleaseError(
                    f'page {page_id} course_totals references a course '
                    'outside the rectangle')
            index = course_index[value]
            if index in seen:
                raise ReleaseError(f'duplicate course_total in page {page_id}')
            seen.add(index)
            course_totals.append(index)
        day_totals = []
        seen = set()
        for value in _array(page['day_totals'], 0, len(days),
                            f'page {page_id} day_totals'):
            if not isinstance(value, str) or value not in day_index:
                raise ReleaseError(
                    f'page {page_id} day_totals references a day outside '
                    'the rectangle')
            index = day_index[value]
            if index in seen:
                raise ReleaseError(f'duplicate day_total in page {page_id}')
            seen.add(index)
            day_totals.append(index)
        pages.append({'page_id': page_id, 'cells': cells,
                      'course_totals': course_totals, 'day_totals': day_totals})
    return {'courses': courses, 'days': days, 'tier': tier,
            'tier_index': TIERS.index(tier), 'protected': protected,
            'pages': pages, 'course_index': course_index,
            'day_index': day_index}


# --- 残量图可达性 ---------------------------------------------------------

def _reachable(adj, cell_out, teleport_out, source, target, drop_cell_edge):
    """残量图中 source 能否到达 target（位集宽度优先）。

    ``drop_cell_edge`` 为真时，从 source 的第一步里去掉指向 target 的那条
    受保护格自身残量边（自由节点之间的中转边不受影响）。简单路径不会中途
    重新经过 source，所以只需改 source 的第一步；target 一旦进入前沿即返回，
    不会展开它的出边，也就不会用到该格的另一条残量边。
    """
    if drop_cell_edge:
        seed = (cell_out[source] & ~(1 << target)) | teleport_out[source]
    else:
        seed = adj[source]
    goal = 1 << target
    visited = (1 << source) | seed
    # 简单路径不会回到 source 再出发，所以 source 本身不进入前沿、不再展开；
    # 自由节点的自环可能把它带进 seed，这里挡掉。
    frontier = seed & ~(1 << source)
    while frontier:
        if frontier & goal:
            return True
        nxt = 0
        pending = frontier
        while pending:
            lsb = pending & -pending
            nxt |= adj[lsb.bit_length() - 1]
            pending ^= lsb
        nxt &= ~visited
        visited |= nxt
        frontier = nxt
    return False


# --- 审查 -----------------------------------------------------------------

def review(snapshot_data, layout_data):
    """校验快照与版面，返回 ``{'forced', 'package'}`` 审查结论。

    ``forced`` 是按 (课程, 日期) 升序的可推出受保护格；为空时 ``package``
    是可直接发布的整包 JSON 字典。调用方据此决定退出码与是否写出。
    """
    snapshot = validate_snapshot(snapshot_data)
    layout = validate_layout(layout_data)
    courses = layout['courses']
    days = layout['days']
    course_index = layout['course_index']
    day_index = layout['day_index']
    rows = len(courses)
    cols = len(days)
    tier_index = layout['tier_index']
    upper = MAX_POINTS // TIER_WEIGHTS[tier_index]

    # 矩形内每格的真实计数；快照没有某桶时该格真实值为零（矩形外桶只校验）。
    values = [[0] * cols for _ in range(rows)]
    for (course_id, day), bucket in snapshot['buckets'].items():
        course = course_index.get(course_id)
        day_index_of = day_index.get(day)
        if course is None or day_index_of is None:
            continue
        values[course][day_index_of] = bucket['counts'][tier_index]

    published_cells = set()
    published_rows = set()
    published_cols = set()
    for page in layout['pages']:
        published_cells.update(page['cells'])
        published_rows.update(page['course_totals'])
        published_cols.update(page['day_totals'])

    # 残量图：未刊登格按真实值给出前向/后向边；未公布合计的行、列是自由
    # 节点，两两可自由中转。
    nodes = rows + cols
    cell_out = [0] * nodes
    for course in range(rows):
        for day in range(cols):
            if (course, day) in published_cells:
                continue
            value = values[course][day]
            if value < upper:
                cell_out[course] |= 1 << (rows + day)
            if value > 0:
                cell_out[rows + day] |= 1 << course
    free_nodes = 0
    for course in range(rows):
        if course not in published_rows:
            free_nodes |= 1 << course
    for day in range(cols):
        if day not in published_cols:
            free_nodes |= 1 << (rows + day)
    teleport_out = [free_nodes if (free_nodes >> node) & 1 else 0
                    for node in range(nodes)]
    adj = [cell_out[node] | teleport_out[node] for node in range(nodes)]

    forced = []
    for course, day in layout['protected']:
        if (course, day) in published_cells:
            forced.append((course, day))
            continue
        value = values[course][day]
        col_node = rows + day
        if value < upper and _reachable(adj, cell_out, teleport_out,
                                        col_node, course, value > 0):
            continue  # 还能加一：至少两个可能值
        if value > 0 and _reachable(adj, cell_out, teleport_out,
                                    course, col_node, value < upper):
            continue  # 还能减一：至少两个可能值
        forced.append((course, day))
    forced.sort()

    row_totals = [sum(values[course]) for course in range(rows)]
    col_totals = [sum(values[course][day] for course in range(rows))
                  for day in range(cols)]
    pages = []
    for page in sorted(layout['pages'], key=lambda item: item['page_id']):
        cells = [{'course_id': courses[course], 'day': days[day],
                  'count': values[course][day]}
                 for course, day in sorted(page['cells'])]
        course_totals = [{'course_id': courses[course],
                          'count': row_totals[course]}
                         for course in sorted(page['course_totals'])]
        day_totals = [{'day': days[day], 'count': col_totals[day]}
                      for day in sorted(page['day_totals'])]
        pages.append({'page_id': page['page_id'], 'cells': cells,
                      'course_totals': course_totals,
                      'day_totals': day_totals})
    package = {'schema_version': SCHEMA_VERSION, 'tier': layout['tier'],
               'courses': list(courses), 'days': list(days), 'pages': pages}
    forced_refs = [[courses[course], days[day]] for course, day in forced]
    return {'forced': forced_refs, 'package': package}

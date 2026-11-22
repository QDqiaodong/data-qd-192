"""阅卷盲测集编排：只消费 v1 报告的数字摘要、提交编号与关联声明。

输入是课程组已经压好的教学合成数据摘要，不读取源字符串、片段内容或切分规则。
输出给出全部可行盲测组集合的精确计数、每个组在所有/部分/零个可行集合中的状态，
以及按升序组表比较后优先包含的规范划分。
"""
from .checks import array, fields, ident, integer, unique

SCHEMA_VERSION = 1
MAX_SUBMISSIONS = 8192
MAX_LINKS = 16384
MAX_GROUPS = 32
MAX_TARGET = 8192
MAX_BASES = 200000
MAX_RECOGNITION_HITS = 20000000
TIERS = ('single', 'light', 'heavy')
SUMMARY_FIELDS = ('bases', 'fragment_bases', 'fragments', 'unique_cuts',
                  'recognition_hits')
ZERO = (0, 0, 0)


class HoldoutError(ValueError):
    """盲测编排的业务/结构错误，CLI 统一以退出码 2 报告。"""


def _tier_index(fragments):
    if fragments == 1:
        return 0
    if fragments <= 7:
        return 1
    return 2


def _validate_summary(summary):
    """摘要接入检查；只核对数字之间的契约，不回溯具体切分过程。"""
    fields(summary, SUMMARY_FIELDS)
    bases = integer(summary['bases'], 1, MAX_BASES)
    fragment_bases = integer(summary['fragment_bases'], 1, MAX_BASES)
    if fragment_bases != bases:
        raise HoldoutError('fragment_bases must equal bases')
    fragments = integer(summary['fragments'], 1, bases)
    unique_cuts = integer(summary['unique_cuts'], 0, bases + 1)
    integer(summary['recognition_hits'], unique_cuts, MAX_RECOGNITION_HITS)
    return _tier_index(fragments)


def validate_batch(data):
    """校验批次并返回 (按 group_id 升序的组表, 三档目标)。

    组表每项为 {'group_id', 'member_ids', 'counts'}；counts 按 TIERS 顺序。
    """
    fields(data, ('submissions', 'links', 'targets'))
    submissions = array(data['submissions'], 1, MAX_SUBMISSIONS)
    tier_of = {}
    ids = []
    for item in submissions:
        fields(item, ('submission_id', 'summary'))
        submission_id = ident(item['submission_id'])
        if submission_id in tier_of:
            raise HoldoutError(f'duplicate submission_id: {submission_id}')
        tier_of[submission_id] = _validate_summary(item['summary'])
        ids.append(submission_id)

    links = array(data['links'], 0, MAX_LINKS)
    parent = {submission_id: submission_id for submission_id in ids}

    def find(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left, right):
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        # 方向无关；按 ID 归并只为让内部结构稳定，不参与输出排序。
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    for link in links:
        if not isinstance(link, list) or len(link) != 2:
            raise HoldoutError('each link must be a two-element array')
        left, right = link
        if not isinstance(left, str) or not isinstance(right, str) \
                or left not in tier_of or right not in tier_of:
            raise HoldoutError('link references an unregistered submission_id')
        union(left, right)

    members_by_root = {}
    for submission_id in ids:
        members_by_root.setdefault(find(submission_id), []).append(submission_id)
    if len(members_by_root) > MAX_GROUPS:
        raise HoldoutError(f'at most {MAX_GROUPS} groups are allowed')

    groups = []
    for member_ids in members_by_root.values():
        member_ids.sort()
        counts = [0, 0, 0]
        for submission_id in member_ids:
            counts[tier_of[submission_id]] += 1
        groups.append({'group_id': member_ids[0], 'member_ids': member_ids,
                       'counts': counts})
    groups.sort(key=lambda group: group['group_id'])

    target_fields = fields(data['targets'], TIERS)
    target = tuple(integer(target_fields[tier], 0, MAX_TARGET)
                   for tier in TIERS)
    return groups, target


def _sum_counts(vectors):
    """枚举一半组的全部子集，返回 {分档和: 子集个数}。

    计数按组身份累计：向量相同的两组仍会被算作两个不同子集。
    """
    counts = {ZERO: 1}
    for dx, dy, dz in vectors:
        updated = counts.copy()
        for (sx, sy, sz), multiplicity in counts.items():
            key = (sx + dx, sy + dy, sz + dz)
            updated[key] = updated.get(key, 0) + multiplicity
        counts = updated
    return counts


def _sum_set(vectors):
    sums = {ZERO}
    for dx, dy, dz in vectors:
        sums = sums | {(sx + dx, sy + dy, sz + dz) for sx, sy, sz in sums}
    return sums


def _nonnegative(vector):
    return vector[0] >= 0 and vector[1] >= 0 and vector[2] >= 0


def _subtract(left, right):
    return (left[0] - right[0], left[1] - right[1], left[2] - right[2])


def _feasible(vectors, target):
    """判断 vectors 的某个子集能否正好凑出 target。"""
    if not _nonnegative(target):
        return False
    if target == ZERO:
        return True
    if not vectors:
        return False
    for dimension in range(3):
        if sum(vector[dimension] for vector in vectors) < target[dimension]:
            return False
    middle = len(vectors) // 2
    left = _sum_set(vectors[:middle])
    right = _sum_set(vectors[middle:])
    # 用小的一侧做迭代，另一侧做常数时间查询。
    if len(left) > len(right):
        left, right = right, left
    for point in right:
        if _nonnegative(_subtract(target, point)) \
                and _subtract(target, point) in left:
            return True
    return False


def _analyze(vectors, target):
    """返回 (可行组集合总数, 每个组所在的可行集合数)。

    折半枚举两侧子集分档和，按补和配对。每个子集只计一次，
    且不展开数量可能极大的可行集合本身。
    """
    count = len(vectors)
    middle = count // 2
    left_vectors = vectors[:middle]
    right_vectors = vectors[middle:]
    left_counts = _sum_counts(left_vectors)
    right_counts = _sum_counts(right_vectors)

    feasible_count = 0
    for right_sum, multiplicity in right_counts.items():
        need = _subtract(target, right_sum)
        if _nonnegative(need):
            feasible_count += multiplicity * left_counts.get(need, 0)
    if feasible_count == 0:
        return 0, [0] * count

    containing = [0] * count
    for index in range(middle):
        vector = left_vectors[index]
        without = _sum_counts(left_vectors[:index] + left_vectors[index + 1:])
        subtotal = 0
        for right_sum, multiplicity in right_counts.items():
            need = _subtract(_subtract(target, right_sum), vector)
            if _nonnegative(need):
                subtotal += multiplicity * without.get(need, 0)
        containing[index] = subtotal
    for offset in range(len(right_vectors)):
        vector = right_vectors[offset]
        without = _sum_counts(
            right_vectors[:offset] + right_vectors[offset + 1:])
        subtotal = 0
        for left_sum, multiplicity in left_counts.items():
            need = _subtract(_subtract(target, left_sum), vector)
            if _nonnegative(need):
                subtotal += multiplicity * without.get(need, 0)
        containing[middle + offset] = subtotal
    return feasible_count, containing


def _canonical_plan(vectors, target):
    """沿升序组表贪心：只要加入当前组仍可完成，就优先包含它。

    这正好产生“两个方案在第一处不同时优先选包含该组”的规范划分；
    每步用折半可行性判定，不依赖已经找到的其它可行集合。
    """
    selected = []
    remaining = target
    for index, vector in enumerate(vectors):
        need = _subtract(remaining, vector)
        if _nonnegative(need) and _feasible(vectors[index + 1:], need):
            selected.append(index)
            remaining = need
    if remaining != ZERO:
        raise HoldoutError('internal error: canonical plan does not meet target')
    return selected


def build_plan(data):
    """校验批次并生成盲测编排结果（可直接作为 JSON 发布）。"""
    groups, target = validate_batch(data)
    vectors = [tuple(group['counts']) for group in groups]
    feasible_count, containing = _analyze(vectors, target)

    if feasible_count == 0:
        statuses = [None] * len(groups)
        blind_group_ids = None
    else:
        statuses = []
        for included_count in containing:
            if included_count == 0:
                statuses.append('excluded')
            elif included_count == feasible_count:
                statuses.append('required')
            else:
                statuses.append('optional')
        blind_group_ids = [groups[index]['group_id']
                           for index in _canonical_plan(vectors, target)]

    return {
        'schema_version': SCHEMA_VERSION,
        'feasible_count': feasible_count,
        'blind_group_ids': blind_group_ids,
        'groups': [
            {
                'group_id': group['group_id'],
                'member_ids': list(group['member_ids']),
                'counts': {TIERS[index]: group['counts'][index]
                           for index in range(3)},
                'blind_status': statuses[index],
            }
            for index, group in enumerate(groups)
        ],
    }

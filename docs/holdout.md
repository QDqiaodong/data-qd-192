# 阅卷盲测集编排 v1

把课程组的作业 v1 报告压成纯数字摘要后，固定一份供阅卷器盲测的数据，其余作为公开练习。编排器只消费数字摘要、提交编号与关联声明，不读取源字符串、片段内容或切分规则，也不提供识别、切分、拼装或真实生物相关能力。

## 输入批次

顶层恰含 `submissions`、`links`、`targets` 三个字段，沿用本仓严格 JSON 读取（8MB、拒绝重复键、拒绝非有限值、布尔值不算整数）。

`submissions` 为 1 至 8192 条，每条恰含 `submission_id` 与原 v1 报告的 `summary` 对象。`summary` 恰含五个整数：

| 字段 | 范围 | 说明 |
| --- | --- | --- |
| `bases` | 1..200000 | 原序列长度 |
| `fragment_bases` | 必须等于 `bases` | 片段长度总和 |
| `fragments` | 1..`bases` | 片段数，决定工作量档 |
| `unique_cuts` | 0..`bases+1` | 合并后的实际切口数 |
| `recognition_hits` | `unique_cuts`..20000000 | 识别命中数 |

这些只是摘要接入检查，不要求从摘要反推具体切分过程；两条提交的数字完全相同也仍是两条提交。按 `fragments` 分档：`single`=1、`light`=2..7、`heavy`>=8。

`links` 为 0 至 16384 个二元数组，引用两个已登记的 `submission_id`，表示课程组确认两条提交来自关联用例、不允许拆到两边。关系无向且可传递；重复边、反向重复与自环不改变结果。合并完成后最多 32 组，独立提交自成一档；一组可跨多个工作量档。`group_id` 取组内字典序最小的提交 ID（ASCII 规则、区分大小写）。

`targets` 恰含 `single`、`light`、`heavy`，各为 0..8192 的整数，是该档要进入盲测的**提交数**（不是组数）。超过该档库存的合法目标表示无解，不是输入错误。

非法结构、重复提交 ID、悬空引用、超过 32 组、读写错误或目标已存在（含符号链接）均退出 2，不留下半个结果、不改动既有文件。

## 输出计划

成功退出 0 并一次性原子新建结果文件，`schema_version` 为 1：

- `groups`：按 `group_id` 升序；每条含 `group_id`、升序 `member_ids`、三档整数 `counts` 与 `blind_status`。
- `feasible_count`：满足目标的不同盲测**组集合**总数（集合身份按组区分，计数相同的两个组不会合并）。
- 有解时 `blind_status` 为 `required`（出现在所有可行集合）、`optional`（部分）、`excluded`（零个）。`blind_group_ids` 给出一份划分：沿升序组表比较两个方案，在第一处不同优先选包含该组的方案，输出被选组的升序 ID。
- 无解时 `feasible_count` 为 0，`blind_group_ids` 与所有 `blind_status` 为 `null`，但仍返回完整组表。
- 三个目标全为 0 时恰有一个空盲测集合：`feasible_count` 为 1，`blind_group_ids` 为 `[]`，所有组为 `excluded`。

重排提交与边、交换边方向、增加重复边不改变结果，包括输出字节。

## 精确结论与规范划分

记每组的 `counts` 为三维向量，问题即计数和为目标的子集。直接枚举全部 2^n 个子集在 32 组时不可行，因此采用**折半枚举 + 补和配对**：

1. 把升序组表任意切成两半，分别枚举所有子集的分档和，得到 `{和: 子集个数}` 表（保留重数，因而相同计数的不同组仍是不同子集）。
2. 全体可行集合数 = Σ 右半表[(s,l,h)] × 左半表[target−(s,l,h)]（补和非负时）。这给出精确整数，不是近似或上限。
3. 对第 i 组，把它从所在半表中去掉重新求“剩余和表”，再用同一补和配对统计包含它的可行集合数；与总数比较即得 `required`/`optional`/`excluded`。因为每次都枚举手头的真实子集，而不是从一个已找到的方案推导，状态判定与是否找到划分无关。

规范划分用**字典序贪心**完成：沿升序组表逐组处理，若把当前组放入盲测后、剩余目标仍可由后续组凑出（一次折半可行性判定），就放入；否则跳过。第一个位置的选择决定了两方案的第一处不同，因此该贪心正好产生“第一处不同优先包含”的规范划分。

折半的关键性质：左右各至多 2^16 个子集，和表最多 2^16 项；即使可行集合数达到 601080390，也从未展开这些集合本身。

## 复杂度

设组数为 g（≤32），计数分布决定和表的实际大小 h（≤2^(g/2)）。

- 时间：主分析做两次半表枚举 O(g·h)，每组状态再各重建一次半表 O(g·h)，规范划分再做 O(g) 次可行性判定 O(g·h)，合计 O(g·h) 级别（常数因重复建表约为 g 的平方）。整数为 Python 大整数，`feasible_count` 不受 64 位限制。
- 空间：两张和表，O(h)；组表、成员表与输出 O(提交数)，不随可行集合数增长。
- 输入解析与并查集 O(提交数 + 边数)。

## 本地复现

```sh
# 直接运行（不安装）
PYTHONPATH=src python3 -P -m sequence_digest.holdout_cli \
  --source fixtures/holdout/acceptance.json --output run/plan.json

# 安装后的入口
sequence-digest-holdout --source BATCH.json --output PLAN.json

# 全部测试（含独立暴力枚举对照与两组 20 秒预算压测）
PYTHONPATH=src python3 -P -m unittest discover -s tests -v

# 单独复现压测（标准库，单进程；计时只覆盖 CLI 读入到计划写完）
python3 -P tools/holdout_bench.py --directory run/bench
```

验收批次 `fixtures/holdout/acceptance.json` 对应链 a—b—c、d—e—f，g 独立，目标 (2,2,2)：唯一解选 a、d，a/d 为 `required`，g 为 `excluded`。

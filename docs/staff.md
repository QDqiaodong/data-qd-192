# 复核产能排班 v1

课程运营组已经能用台账 `sequence-digest-ledger snapshot` 得到各课程、各结算日
还剩多少复核点。本功能把这些点排给各助教队：先草拟预约，再把其中一部分确认给
各队；需求更正后重排，**已确认的部分不能收回**，**也不能把另一门课的剩余缺口
说成已覆盖**，可改的部分先尽量消化需求，再尽量少动原草案。

排的是**可按整数点拆给不同助教队的产能**，不是逐份作业派单。只消费教学合成
数字、日期与业务编号：台账快照的 stdout JSON 与一份排班花名册。不读取完整切分
报告、源字符串、片段内容或切分规则，不新增或改动任何识别、切分及生物相关能力。
运行只依赖 Python 标准库。

```sh
sequence-digest-staff --snapshot SNAPSHOT.json --roster ROSTER.json --output PLAN.json
```

`SNAPSHOT.json` 是 `sequence-digest-ledger snapshot` 的 stdout 原样重定向；本命令
不读台账内部文件，也不自行拉取更晚版本——计划绑定传入快照的 `head`。

## 输入

### 快照

沿用台账快照结构：`schema_version` 必须为 `1`；`head` 是非负整数；`buckets`
至多 8192 条、由 `course_id/day` 唯一确定且互不重复。每条 `counts` 恰有
`single/light/heavy` 三个非负整数、非全零，`points` 等于三档分别乘 1、3、6 后
之和，且不超过 1000000。结算日在计划期外的合法桶合法但不参与排班。

### 花名册

顶层恰含 `start_day`、`end_day`、`shifts`、`bookings`。计划期含首尾两日，最多
31 个自然日。

- `shifts` 至多 2000 条。每条含唯一 `shift_id`、计划期内的 `day`、至多 256 个
  互不重复的合格课程编号 `courses`（仓库 ASCII ID 规则，区分大小写）与
  `capacity`（0..1000000）。一个班次代表一支队在一天内可用的点数产能。
- 班次只能服务名单内、且结算日**不早于**班次日期的桶。
- `bookings` 至多 12000 条，每条恰含 `shift_id/course_id/day/points/locked_points`，
  按前三项唯一。`day` 是目标结算日，须在计划期内；`points` 是 1..1000000 的
  原草案点数，`locked_points` 是其中已确认的部分（0..`points`）。
- 计划期内正需求桶与「有资格且日期合规」班次的配对总数至多 12000，超过即拒绝。
  没有班次、资格或需求都是正常情况。

### 确认的合法性

- **正数确认**必须匹配仍存在、仍合格、且日期合规的班次；否则退出 2。
- 同一班次的确认之和超过它的现有容量也退出 2。
- **未确认预约**（`locked_points == 0`）引用已撤下的班次、已不合格的配对或已
  消失的桶时，**不是输入错误**：可以全部重排或消失。
- **目标桶从最新快照消失，并不使一个本身仍合格的确认失效**：确认照原班次、
  原课程、原结算日保留，只是全部记为确认超额。

## 排班契约

- 所有确认点原班次、原课程、原结算日保留，并**全额消耗原班次容量**。
- 对一个目标桶，先用确认点抵扣最新需求；确认超过需求的部分只记为该桶的
  **确认超额**（`locked_surplus_points`），既不能转给别的桶，也不能省出班次产能。
- 对剩余需求安排**可改点数**：不得造成超额，不得超班次容量，不得跨资格或违反
  日期约束。一个新班次-桶配对不要求出现在旧预约中。
- **第一目标**：最大化实际覆盖的需求点数（`covered_points`）。
- **第二目标**：在第一目标不降的前提下，最大化原配对上保留的未确认点数
  （`retained_provisional_points`）。每个原配对只计新排可改点数与原
  `points - locked_points` 的较小值；迁去别处的点和确认点都不算保留。

同分最优方案不必与其他实现一致，但同一实现对同值输入（桶、班次、资格名单、
预约无论怎样重排）必须产生**相同字节**。

## 输出

成功时**一次性新建** `PLAN.json`，包含：

- `schema_version: 1`、`head`（传入快照的 `head`）。
- `assignments`：只列正数行，沿用预约五字段；`points` 是新总预约点数
  （确认 + 新排可改点），`locked_points` 是原确认点数。按
  `shift_id/course_id/day` 排列。
- `buckets`：按 `course_id/day` 排列，列出计划期内所有正需求桶，以及零需求但
  仍有确认的桶；行内为
  `course_id/day/demand_points/covered_points/shortage_points/locked_surplus_points`。
- `covered_points`：各桶 `covered_points` 之和。
- `retained_provisional_points`：按第二目标计的保留点数。

缺口（`shortage_points > 0`）是合法计划，不是运行错误。

## 求解与资源

排班写成一次**最小费用最大流**：班次残量 → 桶剩余需求是容量边；原配对上的
「保留额度」拆成一条费用 -1 的边，其余容量费用 0。最小费用最大流先保证流量
最大（第一目标），再让 -1 边流量最大（第二目标）。

资源用量随两个量增长：

- **点数**只作为边的容量参与整数流，**不随机进位**：把全部点数乘以 1000 不会
  增加求解步骤，也不会被截断、采样或近似（第 2 条容量验收正是如此）。
- **合法配对数量**（至多 12000）决定图的规模与边数；求解时间与内存随配对数与
  班次数增长，与整数点数的绝对大小无关。

## 错误

输入、文件或确认冲突错误**退出 2**，诊断写 stderr，不留下部分计划，不改动任一
输入或已存在目标（含符号链接）。沿用本仓严格 JSON 边界：两份输入各 8 MB 上限，
拒绝重复键、`NaN`/`Infinity`、多余字段，以及把布尔值当整数。

## 本地复现

### 检查两个目标的优先顺序

```sh
export PYTHONPATH=src
python3 -P -m sequence_digest.ledger_cli apply \
  --store run/ledger.json --source fixtures/staff/ledger-initial.json
python3 -P -m sequence_digest.ledger_cli snapshot --store run/ledger.json  # Blue=9
python3 -P -m sequence_digest.ledger_cli apply \
  --store run/ledger.json --source fixtures/staff/ledger-correction.json
python3 -P -m sequence_digest.ledger_cli snapshot --store run/ledger.json \
  > run/snapshot.json                                                  # Blue=6, Red=6
python3 -P -m sequence_digest.staff_cli \
  --snapshot run/snapshot.json --roster fixtures/staff/roster.json \
  --output run/plan.json
```

期望：F 保留 Blue 的 3 点、把余下 3 点给 Red，B1 保留 Blue 的 3 点，B0 不领
任务；`covered_points=9`、`retained_provisional_points=3`、Red `shortage=3`。
若直接尽量保住旧草案，就达不到覆盖 9。

把 `fixtures/staff/roster.json` 里 F 那一行的 `locked_points` 改为 0（草案总量、
班次、需求不变）重跑：Red 应得到 6，全局覆盖 12，且在此覆盖下仍保住 B1 的 3 点。
再用原确认量并在台账撤销 Blue 的剩余需求：F 的 3 点仍排在 Blue 并全部列为
确认超额，Red 仍缺 3。

### 重现容量验收

```sh
python3 -P tools/staff_capacity.py --directory run/staff-capacity
```

它在受限进程之外生成 128 份互不共享班次与课程的副本（所有点数乘 1000、编号加
前缀、同一计划期），再用 256 MiB 虚拟地址空间上限运行真实 CLI 从读取到写出，
最后逐桶、逐分配核对：输出与单副本第 1 条结果一致，覆盖共 1152000、保留共
384000。求解不转交子进程或外部服务。

### 全部测试

```sh
PYTHONPATH=src python3 -P -m unittest discover -s tests -v
```

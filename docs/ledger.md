# 人工复核工作量台账与勘误流 v1

课程组已经能把教学作业压成 v1 报告的数字摘要，但人工复核工时按**课程**和**结算日记账**，而助教会补发更正：同一份提交可能改到另一门课、换一个结算日，或被撤销。本功能维护一份可持续接入的台账，并对外提供快照与增量勘误流。

本模块只消费 `course_id`、`submission_id`、`day`、`revision` 与 v1 报告 `summary` 的五个整数；不打开完整报告，不读源字符串、片段内容或切分规则，不接入真实样本或外部服务，也不改变任何识别/切分功能。运行只依赖 Python 标准库。

## 工作量口径

沿用盲测编排的摘要接入校验与三档口径（`holdout.validate_summary`）：

| `fragments` | 档位 | 复核工作量点数 |
| --- | --- | --- |
| 1 | `single` | 1 |
| 2..7 | `light` | 3 |
| ≥8 | `heavy` | 6 |

点数只用于课程排班，不是工时或薪资口径。台账与勘误记录的是**提交数**按档的计数，点数由计数推导。

## 批次输入

顶层恰含 `records`，为 1..8192 条；同一 `submission_id` 在一批内只能出现一次。每条：

- `submission_id`：仓库 ASCII ID 规则 `[A-Za-z0-9_:-]{1,64}`，区分大小写。
- `revision`：1..2147483647 的整数（布尔或浮点不算整数）。
- `op`：`put` 或 `cancel`。
  - `put` 还必须恰有 `course_id`（同上 ID 规则）、`day`（严格 `YYYY-MM-DD` 的合法公历日期）与 `summary`（完整 v1 数字摘要，五个整数字段及其范围/关系校验与盲测编排一致）。
  - `cancel` 不得带 `course_id`、`day`、`summary`。

沿用本仓严格 JSON 边界（`io.load`）：8 MB 上限、拒绝重复键、拒绝 `NaN`/`Infinity`。课程与结算日以记录所填为准，不从到达时间推导。

**每条记录都会被校验，即使它最终过时。** 批中任何错误使所有记录均不生效。

## 版本规则

每个提交独立比较该台账已接纳的最高版本：

- 更高版本完整替代旧版本，允许跳号。
- 较低版本不改变事实（仍是合法输入，正常校验后被忽略）。
- 版本相同：约定字段值完全相同则无动作；字段值不同则拒绝整批，即使两条记录会算出一样的点数。比较的是字段值，不是 JSON 字节或键顺序。
- 首次见到的 `cancel` 也记住版本（没有可扣工作量），后续更高版本的 `put` 可恢复该提交。

## 净变动与勘误

一个有效 `put` 只在其 `(course_id, day)` 桶贡献一个分档计数与相应点数；已撤销的提交不贡献。

每批对外只产生相对于**接入前**台账的净变动：

- 同桶轧差；所有分档增量均为零的桶不输出。
- 同桶换档即使总提交数不变，也保留分档差值。
- 若所有桶净变动全为零，仍接纳新版本，但不产生勘误、不推进游标。
- 否则整批产生且仅产生一条勘误，游标从 1 连续递增。一条勘误可含多个桶，桶按课程 ID、日期升序输出。
- 批次内记录顺序不影响结果（含输出字节）。
- 已提交的勘误不会因后续接入而改写或消失。

## 命令

```sh
# 接入一个批次（首次有效 apply 初始化台账）
sequence-digest-ledger apply --store PATH --source BATCH.json

# 读取当前桶表与游标
sequence-digest-ledger snapshot --store PATH

# 按游标拉取勘误页
sequence-digest-ledger changes --store PATH --after N --limit L
```

三条命令成功时各向 stdout 输出一个 JSON 对象，均带 `schema_version: 1`。

### 桶行与勘误

桶行含 `course_id`、`day`、`counts`（三档整数）与 `points`。在勘误中后两者是**有符号增量**，在快照中是**当前值**；快照不列全零桶。

- `apply` → `{schema_version, head, emitted}`：`head` 是接入后的勘误游标，`emitted` 表示本批是否产生勘误。
- `snapshot` → `{schema_version, head, buckets}`。
- `changes` → `{schema_version, head, next, entries}`：从严格大于 `after` 的游标起升序取最多 `limit` 条完整勘误，每条含 `cursor` 与 `deltas`；跨桶的同一条勘误不会被拆页。`next` 是本页最后一条的游标，空页原样返回 `after`。要求 `0 <= after <= head`、`1 <= limit <= 100`。

尚无台账时，查询视为**游标 0 的空台账**（`snapshot` 返回空 `buckets`，`changes` 返回空页）。

### 仪表盘怎样从快照继续追增量

1. 首次接入：调用 `snapshot`，以返回的 `head` 作为游标，`buckets` 作为本地桶表。
2. 之后循环调用 `changes --after <游标> --limit L`，把每条 `entries[*].deltas` 的有符号增量并进本地桶表（某桶三档计数都归零时移除该桶），令游标 = `next`。
3. 当 `entries` 为空时页面结束，游标不变；继续轮询即可。

快照的 `head` 与 `buckets` 取自同一个已提交状态，因此即使两次命令之间写入了新批次，以该 `head` 为起点也能接上全部后续净变动。测试同时从**空台账**和从**游标 1 的旧快照**用 `limit=1` 拉页，两者都追到同一当前桶表。

## 提交可见边界与中断安全

- 台账内部表示自定（一个 JSON 文件），**一次 `os.replace` 原子替换**发布。读者看到的永远是某个完整已提交状态：桶表与勘误游标互相对应。
- 写者先把完整新状态（提交版本、桶表、新勘误）写入同目录临时文件并 `fsync`，再原子替换。进程在批中任意位置被终止时，旧状态保持原样；新状态要么整体可见，要么完全不可见，绝不会出现"版本已记、工时未扣"之类的中间态。
- 新进程直接读取上一次的有效台账继续工作，不依赖原输入批次，也不需要人工清理；残留的临时文件会在下一次有效提交（例如重新接入同一批）时清理，查询从不读取它们。
- 设计目标是**一个写者 + 同时查询的读者**，不要求多个写者或断电恢复。
- 非法输入、版本冲突、非法游标或读写失败退出 2，向 stderr 给诊断，不输出成功 JSON。

## 测试中的中断复现

`ledger.apply_batch(..., interrupt=...)` 是进程内测试钩子，在临时状态已 `fsync`、正式替换尚未发生时调用。CLI 通过环境变量暴露同一位置，便于复现真实进程中止：

```sh
# 写者在提交点写出标记文件，然后阻塞等待 stdin
SEQUENCE_DIGEST_LEDGER_TEST_PAUSE_FILE=/tmp/paused \
  sequence-digest-ledger apply --store run/ledger.json --source CORRECTION.json
# 此时另起进程读取，仍是旧桶与旧游标；随后终止写者
```

正常使用不设置该变量，行为完全不变。`tests/test_ledger.py::InterruptionTests` 正是：启动写者→等待标记→另起读者确认旧状态→`SIGTERM` 终止→新进程再次查询旧状态→重新接入同批得到游标 2 与整条勘误；并覆盖写者正常完成时旧快照游标继续拉取不漏勘误。该测试不是"开始写入前拒收输入"的替身：中断发生在完整新状态已落盘临时介质之后。

## 夹具与本地复现

`fixtures/ledger/` 下的 `initial.json` 与 `correction.json` 为验收第 1 条的合成批次：

- `initial.json`：A v1 → blue/2026-11-10（light），B v1 → red/2026-11-10（single）。游标 1。
- `correction.json`：A v3 → red/2026-11-11（heavy），B v2 → blue/2026-11-10（light）。游标 2 的那一条勘误只含 red/2026-11-10 的 `single -1`/`-1` 点与 red/2026-11-11 的 `heavy +1`/`+6` 点；blue/2026-11-10 的旧 A 与新 B 轧差为零，不出现。

```sh
export PYTHONPATH=src
python3 -P -m sequence_digest.ledger_cli apply \
  --store run/ledger.json --source fixtures/ledger/initial.json
python3 -P -m sequence_digest.ledger_cli snapshot --store run/ledger.json
python3 -P -m sequence_digest.ledger_cli apply \
  --store run/ledger.json --source fixtures/ledger/correction.json
python3 -P -m sequence_digest.ledger_cli changes \
  --store run/ledger.json --after 1 --limit 1
python3 -P -m sequence_digest.ledger_cli changes \
  --store run/ledger.json --after 2 --limit 1   # 空页，next=2
```

全部测试：

```sh
PYTHONPATH=src python3 -P -m unittest discover -s tests -v
```

# data-qd-192 · 线性与环状序列切分

给定识别串与切口偏移，识别双方向和跨源点命中，合并实际切口，再生成有坐标与哈希凭据的片段序列。

recognize.py 映射方向和切口；fragments.py 根据线性/环状弧生成真实片段；service.py 核对碱基长度守恒。 详细字段、单位和失败边界见 [数据契约](docs/contract.md)。全部样例为自制合成数据，许可见 [来源](fixtures/SOURCES.md)。

## 独立安装与演示

Python >=3.11，本批实际使用CPython3.12.14，运行/测试仅标准库。每仓建立独立.venv，不共享其他项目运行环境；复用公共构建缓存，锁定setuptools80.9.0/wheel0.45.1。以下命令已在本机已有uv、Python与公共缓存时实际离线执行通过；其他机器首次缺缓存时需准备依赖。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/sequence-digest --source fixtures/input.json --output run/report-initial.json
```

实际6项测试通过，演示摘要：

```json
{
  "bases": 8,
  "fragment_bases": 8,
  "fragments": 2,
  "recognition_hits": 2,
  "unique_cuts": 2
}
```

报告原子新建，不覆盖旧文件或符号链接。演示路径已存在时请改用新输出名；输入结构/约束或文件错误退出2，正常业务诊断见契约。已实际核验重复JSON键拒绝且不生成结果、同名输出字节不变及符号链接不覆盖。

## 坐标血缘发布包

新增 `sequence-digest-lineage` 入口，沿用同一套 v1 切分结果，只做坐标投影、身份绑定与归档重发；发布数据与归档都不含源字符串、片段字符或切分规则。字段、坐标约定与崩溃恢复语义见 [血缘契约](docs/lineage.md)。

```sh
# 封存 R1/R2（fixtures/lineage 为同一环状合成串的两套现有规则配置与共用标注）
sequence-digest-lineage seal --source fixtures/lineage/source-r1.json \
  --annotations fixtures/lineage/annotations-ring.json \
  --archive run/archive --release-id R1
# stdout 一行回执，仅含 release_id 与 export_sha256，由使用方独立留存
sequence-digest-lineage seal --source fixtures/lineage/source-r2.json \
  --annotations fixtures/lineage/annotations-ring.json \
  --archive run/archive --release-id R2

# 源输入撤下、归档搬迁后，仅凭归档与回执重发
sequence-digest-lineage export --archive run/archive \
  --receipt run/R1-receipt.json --output run/R1-release.json
```

同一 `release_id` 重复封存内容相同则返回同一回执，内容不同则退出2且不动旧发布；回执哈希错误、归档缺损或导出目标已存在（含符号链接）均退出2且不留部分文件。

## 阅卷盲测集编排

新增 `sequence-digest-holdout` 入口：课程组把 v1 报告压成纯数字摘要后，用它固定一份可盲测的组集合，并说明每组是必进、可调还是不可进。只消费摘要、提交编号与关联声明，不读取源字符串、片段内容或切分规则。字段、精确计数方法与复杂度见 [盲测编排契约](docs/holdout.md)。

```sh
sequence-digest-holdout --source fixtures/holdout/acceptance.json --output run/plan.json
# 或无需安装：
PYTHONPATH=src python3 -P -m sequence_digest.holdout_cli \
  --source BATCH.json --output PLAN.json
```

结果原子新建并拒绝覆盖已有目标（含符号链接）；输入非法或超过组数上限退出2。已知无解也是成功结果（`feasible_count` 为 0，状态为空）。两组 20 秒预算压测可独立复现：

```sh
python3 -P tools/holdout_bench.py --directory run/bench
```

## 人工复核工作量台账与勘误流

新增 `sequence-digest-ledger` 入口：维护按课程与结算日的复核工作量台账，并对外提供快照与不可改写的增量勘误流。只消费课程编号、提交编号、日期、版本号与 v1 报告的五个整数摘要，不打开完整报告，也不改变任何识别/切分功能。字段、版本规则与中断语义见 [台账契约](docs/ledger.md)。

```sh
sequence-digest-ledger apply --store run/ledger.json --source fixtures/ledger/initial.json
sequence-digest-ledger snapshot --store run/ledger.json
sequence-digest-ledger apply --store run/ledger.json --source fixtures/ledger/correction.json
sequence-digest-ledger changes --store run/ledger.json --after 1 --limit 1
```

每次有效接入只产生一条相对接入前台账的净勘误；同桶轧差、跨桶换档保留分档差值。仪表盘以 `snapshot` 的 `head` 为起点，用 `changes` 的 `deltas` 逐页追增量。单写者、原子替换：写者中途终止时新版本、桶表与勘误要么全部可见，要么都不可见。

## 测试

不安装包也可直接用标准库运行（`-P` 避免当前目录同名模块干扰）：

```sh
PYTHONPATH=src python3 -P -m unittest discover -s tests -v
```

当前88项测试覆盖 v1 既有报告与错误行为、线性首尾边界、环状整圈标注、错版哈希拒绝、R1/R2 封存-搬迁-新进程重发、回执篡改与归档损坏、写入失败后原 ID 重试、盲测编排的验收场景、独立暴力枚举对照、非法输入与两组压测，以及人工复核台账的净变动、版本规则、撤销/恢复、从空态与旧快照的增量对齐和写者中断复现。

## 当前能力与后续集成

明确的单字符串切分模型；不模拟双链突出端、实验条件或真实酶数据库。 合理新增需求可沿现有身份和结果接入连接拼装、跨切口特征追溯与序列编辑；当前尚未实现这些扩展，不保证固定数量未来题目。

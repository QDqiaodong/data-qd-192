# 坐标血缘发布包（lineage v1）

面向教学切分的坐标发布：上游课程编辑器在自己的外部坐标系标注字符，下游阅卷器只拿到发布坐标，收不到课堂原始字符串、片段字符内容或切分规则。切分本身完全沿用 v1（`service.process`），本功能只处理坐标投影、业务身份绑定与归档重发，不生成新字符串，也不接真实样本、外部数据库或服务。

## 输入与坐标约定

- v1 源输入与 `sequence-digest --source ... --output ...` 的输入完全相同，v1 报告保持不变。
- 标注 JSON 为单个对象，字段固定为：
  - `molecule_id`、`topology`（`linear`/`circular`）、`length`（源长度）、`sequence_sha256`（源字符串 ASCII 字节的 64 位小写十六进制 SHA-256）、`origin`、`labels`。
  - 前四项必须与实际切分的源逐项吻合；同名同长度但哈希不同的错版数据一律拒绝投影。
  - 外部坐标 0 对应源坐标 `origin`。线性源 `origin` 必须为 0；环状源 `origin ∈ [0,n)`。外部位置 `p` 对应源位置 `(origin+p) mod n`，方向不变。
  - 每条标注含唯一 `id`（仓库 ID 规则 `[A-Za-z0-9_:-]{1,64}`）、外部 `start ∈ [0,n)` 与正整数 `length`。线性标注不得超过末端；环状允许绕过外部零点，但最长一圈（`length ≤ n`）。
  - 标注重叠与空标注集均允许；最多 1000 条，标注长度之和不超过 200000。
  - 沿用仓库严格 JSON 读取：拒绝重复键、错字段，布尔值不被当作整数。

## 发布 JSON 契约

稳定 UTF-8 字节流：`json.dumps(ensure_ascii=False, sort_keys=True, indent=2)` 后追加换行，跨进程、跨路径字节不变。字段：

- `schema_version`（当前为 1）、`release_id`、`molecule_id`、`topology`、`length`、`sequence_sha256`、`origin`。
- `fragments`：沿 v1 报告顺序列出全部片段，即使没有任何标注命中也保留；每项只含 `fragment_id`、`length`、`circular` 与有序 `source_spans`（半开区间，跨源点片段有两个区间）。
- `labels`：按 `id` 排列，保留原 `id/start/length`；`mappings` 沿标注自身正向走向排列。每条映射：
  - `label_span`：相对标注首字符的局部半开区间；
  - `source_span`：源坐标系中的半开区间；
  - `fragment_id`：命中的片段；
  - `fragment_span`：片段局部半开区间，片段局部坐标按该片段 `source_spans` 的顺序累加，不按源坐标排序。
- 三个区间等长且半开；一条记录不会跨出目标片段的同一个 `source_spans` 区间。外部坐标经过 0 本身不拆记录，只有源零点或片段 `source_spans` 边界才拆。
- 同一标注的所有 `label_span` 恰好首尾相接覆盖其长度；绕一圈后再次落到同一片段（的另一区间）也保留为两条记录，不合并。
- 发布数据与归档均不含源字符串、片段字符（无 `sequence`/片段哈希）或任何切分规则（无 motif、酶、切口）。

## 命令

```sh
# 封存：成功时 stdout 输出一行 JSON 回执，仅含 release_id 与 export_sha256
sequence-digest-lineage seal \
  --source FILE --annotations FILE --archive DIR --release-id ID

# 重发：只需要归档与使用方独立留存的回执
sequence-digest-lineage export \
  --archive DIR --receipt FILE --output FILE
```

`export_sha256` 对应该发布实际可导出的完整 UTF-8 JSON 字节。结构/业务错误一律退出码 2。

## 归档内部表示

封存后归档目录布局：

```
<archive>/<release_id>/
    release.json   # 规范发布字节，export 的唯一数据来源
    receipt.json   # 归档内回执副本（release_id + export_sha256）
    manifest.json  # archive_format、文件名与 release_sha256 元数据
```

- 实际保留：坐标、业务身份、长度、源哈希与发布字节哈希。不保留、也无法还原源字符串或切分规则。
- 重发依赖：仅依赖归档中的 `release.json` 与使用方独立留存的回执；源输入、标注输入在封存后即可撤下或更换。归档可整体搬迁，并以只读方式使用（export 不写归档）。
- 缺损检测：export 先读取归档发布字节，计算 SHA-256 并与**回执中的** `export_sha256` 比对，绝不拿归档内临时算出的哈希替代回执；再校验 JSON 结构与 `release_id` 一致。字节被篡改、文件缺失、回执错误或归档被搬到不一致的副本，都会在写出任何输出前退出 2。

## 封存原子性与中断恢复（单一写者）

- 封存先在归档内创建 `.staging-*` 临时目录，依次写入发布、回执、清单并 fsync，最后用一次原子 `os.replace` 改名为正式的 `<release_id>` 目录；正式目录要么完整出现，要么不存在。
- 同一 `release_id` 在一个归档内不可改绑：再次 seal 内容相同则返回同一回执；内容不同则报错，旧发布字节不变。
- 无效输入在校验阶段失败，不会创建占住 ID 的正式目录。
- 单一写者场景下，若进程在封存中途终止：重启后下次 seal 会发现并清理残留的 `.staging-*` 目录（正式目录从未改名成功），随后可用原 ID 重新封存。本方案不处理多写者并发协调，也不依赖服务器或机器掉电保证。

## 重发的输出安全

- 导出目标已存在（包括符号链接）即退出 2，不覆盖、不改既有文件；新文件经临时文件 fsync 后硬链接发布，失败不留部分文件，归档保持不变。
- 归档内发布字节与回执哈希不一致时拒绝发布，因此把 R2 的哈希换进 R1 回执、或损坏 R1 发布数据，都不会产出文件，也不会把 R2 的坐标代给 R1。

## 夹具来源

`fixtures/lineage/` 下的源输入与标注均为作者自制合成数据（同一环状 8 字符教学串），不来自客户、真实样本或生产作业。

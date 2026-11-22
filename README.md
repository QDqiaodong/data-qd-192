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

## 当前能力与后续集成

明确的单字符串切分模型；不模拟双链突出端、实验条件或真实酶数据库。 合理新增需求可沿现有身份和结果接入连接拼装、跨切口特征追溯与序列编辑；当前尚未实现这些扩展，不保证固定数量未来题目。

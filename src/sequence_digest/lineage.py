"""坐标血缘发布：把外部标注坐标投影到一次 v1 切分的片段，并封存可重发的发布包。

本模块只处理坐标、业务身份与归档：
- 切分结果完全沿用 service.process 的 v1 逻辑，不另写识别/切分规则；
- 发布数据与归档都不复制源字符串、片段字符内容或切分规则；
- 重发只依赖归档目录与使用方独立留存的回执。
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .checks import fields, ident, integer, unique
from .io import load
from .model import validate
from .service import process

SCHEMA_VERSION = 1
ANNOTATION_KEYS = ('molecule_id', 'topology', 'length', 'sequence_sha256', 'origin', 'labels')
LABEL_KEYS = ('id', 'start', 'length')
MAX_LABELS = 1000
MAX_TOTAL_LABEL_LENGTH = 200000
ARCHIVE_FORMAT = 1
STAGING_PREFIX = '.staging-'
RELEASE_FILE = 'release.json'
RECEIPT_FILE = 'receipt.json'
MANIFEST_FILE = 'manifest.json'


class LineageError(ValueError):
    """血缘发布的业务/结构错误，CLI 统一以退出码 2 报告。"""


def _sha256_ascii(text):
    return hashlib.sha256(text.encode('ascii')).hexdigest()


def _validate_annotations(data, molecule):
    fields(data, ANNOTATION_KEYS)
    molecule_id = data['molecule_id']
    topology = data['topology']
    length = data['length']
    digest = data['sequence_sha256']
    origin = data['origin']
    labels = data['labels']
    ident(molecule_id)
    if topology not in ('linear', 'circular'):
        raise LineageError('linear/circular topology required')
    integer(length, 1, 200000)
    if not isinstance(digest, str) or len(digest) != 64:
        raise LineageError('sequence_sha256 must be a 64-character hex string')
    digest_lower = digest.lower()
    if digest_lower != digest or any(c not in '0123456789abcdef' for c in digest):
        raise LineageError('sequence_sha256 must be lowercase hexadecimal')
    integer(origin, 0, length - 1)
    if topology == 'linear' and origin != 0:
        raise LineageError('linear origin must be 0')
    if not isinstance(labels, list) or len(labels) > MAX_LABELS:
        raise LineageError(f'labels must be a list of at most {MAX_LABELS} entries')
    label_ids = []
    total = 0
    checked = []
    for label in labels:
        fields(label, LABEL_KEYS)
        label_id = label['id']
        start = label['start']
        label_length = label['length']
        ident(label_id)
        integer(start, 0, length - 1)
        integer(label_length, 1, MAX_TOTAL_LABEL_LENGTH)
        if topology == 'linear' and start + label_length > length:
            raise LineageError('linear label extends past source end')
        if topology == 'circular' and label_length > length:
            raise LineageError('circular label may wrap at most one full turn')
        total += label_length
        if total > MAX_TOTAL_LABEL_LENGTH:
            raise LineageError(f'total label length exceeds {MAX_TOTAL_LABEL_LENGTH}')
        label_ids.append(label_id)
        checked.append({'id': label_id, 'start': start, 'length': label_length})
    unique(label_ids)
    # 源绑定四项必须与实际切分的源完全吻合，防止对错版数据投影。
    if molecule_id != molecule['id']:
        raise LineageError('annotation molecule_id does not match the digested source')
    if topology != molecule['topology']:
        raise LineageError('annotation topology does not match the digested source')
    if length != len(molecule['sequence']):
        raise LineageError('annotation length does not match the digested source')
    if digest_lower != _sha256_ascii(molecule['sequence']):
        raise LineageError('annotation sequence_sha256 does not match the digested source')
    return digest_lower, origin, checked


def _fragment_lookup(fragments, n):
    """返回按源坐标索引的 (片段序号, 片段内第几个 source_span, 片段内局部坐标)。"""
    lookup = [None] * n
    for index, fragment in enumerate(fragments):
        local = 0
        for span_index, span in enumerate(fragment['source_spans']):
            lo, hi = span
            for position in range(lo, hi):
                if lookup[position] is not None:
                    raise LineageError('fragment source_spans overlap; refusing projection')
                lookup[position] = (index, span_index, local)
                local += 1
        if local != fragment['length']:
            raise LineageError('fragment local coordinates do not cover its length')
    if any(entry is None for entry in lookup):
        raise LineageError('fragment source_spans do not cover the whole source')
    return lookup


def _walk_intervals(start, length, n, circular):
    """把外部坐标走向切成源坐标系中的连续半开区间（源零点处断开）。"""
    intervals = []
    position = start
    remaining = length
    while remaining:
        if circular:
            end = min(n, position + remaining)
        else:
            end = position + remaining
        intervals.append((position, end))
        step = end - position
        remaining -= step
        position = 0 if circular else end
    return intervals


def _project_label(label, origin, n, circular, fragments, lookup):
    start = label['start']
    length = label['length']
    source_start = (origin + start) % n
    records = []
    label_offset = 0
    groups = []  # (fragment_index, span_index, source_lo, source_hi, label_lo, label_hi)
    for lo, hi in _walk_intervals(source_start, length, n, circular):
        cursor = lo
        while cursor < hi:
            fragment_index, span_index, _ = lookup[cursor]
            _, span_hi = fragments[fragment_index]['source_spans'][span_index]
            end = min(hi, span_hi)
            groups.append((fragment_index, span_index, cursor, end))
            cursor = end
    # 合并同一片段同一 source_span 内的连续访问；绕圈再次落入则保留为两条记录。
    merged = []
    for fragment_index, span_index, lo, hi in groups:
        if merged and merged[-1][0] == fragment_index and merged[-1][1] == span_index \
                and merged[-1][3] == lo:
            previous = merged[-1]
            merged[-1] = (fragment_index, span_index, previous[2], hi)
        else:
            merged.append((fragment_index, span_index, lo, hi))
    for fragment_index, span_index, lo, hi in merged:
        fragment = fragments[fragment_index]
        record_length = hi - lo
        _, _, local_lo = lookup[lo]
        records.append({
            'label_span': [label_offset, label_offset + record_length],
            'source_span': [lo, hi],
            'fragment_id': fragment['fragment_id'],
            'fragment_span': [local_lo, local_lo + record_length],
        })
        label_offset += record_length
    if label_offset != length or not records:
        raise LineageError('label projection failed to cover the full label length')
    cursor = 0
    for record in records:
        if record['label_span'] != [cursor, cursor + (record['source_span'][1] - record['source_span'][0])]:
            raise LineageError('label_span intervals are not contiguous')
        cursor = record['label_span'][1]
    if cursor != length:
        raise LineageError('label_span coverage gap')
    return records


def build_release(source_data, annotations, release_id):
    """由 v1 输入与标注对象构造发布对象（dict，不含任何字符串/切分规则内容）。"""
    ident(release_id)
    try:
        molecule, enzymes = validate(source_data)
        report = process(source_data)
        fragments = report['fragments']
        n = len(molecule['sequence'])
        digest, origin, labels = _validate_annotations(annotations, molecule)
        circular = molecule['topology'] == 'circular'
        lookup = _fragment_lookup(fragments, n)
        published_fragments = [
            {
                'fragment_id': fragment['fragment_id'],
                'length': fragment['length'],
                'circular': fragment['circular'],
                'source_spans': [list(span) for span in fragment['source_spans']],
            }
            for fragment in fragments
        ]
        published_labels = []
        for label in sorted(labels, key=lambda item: item['id']):
            published_labels.append({
                'id': label['id'],
                'start': label['start'],
                'length': label['length'],
                'mappings': _project_label(label, origin, n, circular, fragments, lookup),
            })
    except LineageError:
        raise
    except ValueError as exc:
        raise LineageError(str(exc)) from exc
    return {
        'schema_version': SCHEMA_VERSION,
        'release_id': release_id,
        'molecule_id': molecule['id'],
        'topology': molecule['topology'],
        'length': n,
        'sequence_sha256': digest,
        'origin': origin,
        'fragments': published_fragments,
        'labels': published_labels,
    }


def canonical_bytes(release):
    """发布字节：稳定 UTF-8 JSON，换进程/换路径序列化结果不变。"""
    return (json.dumps(release, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False, separators=(',', ': ')) + '\n').encode('utf-8')


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_payload(directory, name, payload):
    target = directory / name
    with tempfile.NamedTemporaryFile(dir=directory, prefix=f'.{name}.', delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def seal(source_path, annotations_path, archive_dir, release_id, fail_writer=None):
    """校验并封存一次发布，返回 (发布字节, 回执 dict)。

    同一归档中 release_id 不可改绑：内容相同返回同一回执，内容不同报错且不动旧发布。
    fail_writer 用于测试在写入发布字节后模拟一次失败，验证半成品可被清理重试。
    """
    ident(release_id)
    archive = Path(archive_dir)
    archive.mkdir(parents=True, exist_ok=True)
    release_dir = archive / release_id
    if release_dir.is_symlink():
        raise LineageError(f'archive release path is a symlink: {release_id}')
    if release_dir.exists() and not release_dir.is_dir():
        raise LineageError(f'archive release path is not a directory: {release_id}')
    source_data = load(source_path)
    annotation_data = load(annotations_path)
    release = build_release(source_data, annotation_data, release_id)
    payload = canonical_bytes(release)
    digest = hashlib.sha256(payload).hexdigest()
    receipt = {'release_id': release_id, 'export_sha256': digest}
    receipt_bytes = canonical_bytes(receipt)

    # 单一写者：上次进程中途终止只可能残留 staging 目录，正式目录只在最后原子改名出现。
    for stale in archive.glob(STAGING_PREFIX + '*'):
        if stale.is_dir() and not stale.is_symlink():
            shutil.rmtree(stale)
        else:
            raise LineageError(f'unexpected archive entry blocks sealing: {stale.name}')
    if release_dir.exists():
        existing_payload = (release_dir / RELEASE_FILE).read_bytes()
        existing_receipt = (release_dir / RECEIPT_FILE).read_bytes()
        if existing_payload != payload or existing_receipt != receipt_bytes:
            raise LineageError(f'release_id {release_id} is already bound to different content')
        return payload, receipt

    staging = Path(tempfile.mkdtemp(dir=archive, prefix=STAGING_PREFIX))
    try:
        _write_payload(staging, RELEASE_FILE, payload)
        if fail_writer is not None:
            fail_writer(staging)
        _write_payload(staging, RECEIPT_FILE, receipt_bytes)
        manifest = canonical_bytes({
            'archive_format': ARCHIVE_FORMAT,
            'release_id': release_id,
            'release_file': RELEASE_FILE,
            'release_sha256': digest,
            'receipt_file': RECEIPT_FILE,
        })
        _write_payload(staging, MANIFEST_FILE, manifest)
        os.replace(staging, release_dir)
        _fsync_directory(archive)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return payload, receipt


def _load_receipt(receipt_path):
    receipt = load(receipt_path)
    fields(receipt, ('release_id', 'export_sha256'))
    ident(receipt['release_id'])
    digest = receipt['export_sha256']
    if not isinstance(digest, str) or len(digest) != 64 or digest != digest.lower() \
            or any(c not in '0123456789abcdef' for c in digest):
        raise LineageError('receipt export_sha256 must be a 64-character lowercase hex string')
    return receipt


def export(archive_dir, receipt_path, output_path):
    """仅凭归档与独立回执重发；字节与回执哈希一致才落盘，成功时返回发布字节。"""
    receipt = _load_receipt(receipt_path)
    archive = Path(archive_dir)
    if not archive.is_dir():
        raise LineageError('archive is missing or not a directory')
    release_dir = archive / receipt['release_id']
    release_path = release_dir / RELEASE_FILE
    if not release_path.is_file() or release_path.is_symlink():
        raise LineageError(f'published release not found in archive: {receipt["release_id"]}')
    payload = release_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != receipt['export_sha256']:
        raise LineageError('export bytes do not match the independently supplied receipt')
    try:
        archived = json.loads(payload.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LineageError(f'archived release is corrupt: {exc}') from exc
    if not isinstance(archived, dict) or archived.get('release_id') != receipt['release_id']:
        raise LineageError('archived release identity does not match the receipt')
    target = Path(output_path)
    # 包括符号链接在内，目标已存在即拒绝，绝不改动既有输出。
    if target.exists() or target.is_symlink():
        raise LineageError(f'export target already exists: {output_path}')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.lineage-export-',
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    return payload

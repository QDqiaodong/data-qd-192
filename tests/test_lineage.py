"""坐标血缘发布包测试：投影契约、封存/重发、归档鲁棒性与旧入口不回归。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from sequence_digest.io import load
from sequence_digest.lineage import (
    LineageError, build_release, canonical_bytes, export, seal,
)
from sequence_digest.service import process

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
FIXTURES = ROOT / 'fixtures' / 'lineage'
R1_SOURCE = FIXTURES / 'source-r1.json'
R2_SOURCE = FIXTURES / 'source-r2.json'
RING_ANNOTATIONS = FIXTURES / 'annotations-ring.json'
V1_SOURCE = ROOT / 'fixtures' / 'input.json'
RING_SEQUENCE = 'AATTCCGG'
RING_SHA256 = hashlib.sha256(RING_SEQUENCE.encode('ascii')).hexdigest()


def run_cli(*args, cwd=None):
    """新进程运行 lineage CLI；-P 保证不加载 cwd 下的同名模块。"""
    env = dict(os.environ)
    env['PYTHONPATH'] = str(SRC)
    return subprocess.run(
        [sys.executable, '-P', '-m', 'sequence_digest.lineage_cli', *args],
        cwd=cwd or str(ROOT), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8')
    return path


def ring_annotations(labels, origin=7):
    return {
        'molecule_id': 'ring',
        'topology': 'circular',
        'length': 8,
        'sequence_sha256': RING_SHA256,
        'origin': origin,
        'labels': labels,
    }


def linear_source(sequence='ACGTACGT', enzymes=None):
    if enzymes is None:
        # 仅在位置 3 命中一次，产生单一切口 4。
        enzymes = [{'id': 'e1', 'motif': 'TACG', 'cut_after': 1}]
    return {'molecule': {'id': 'lin', 'topology': 'linear', 'sequence': sequence},
            'enzymes': enzymes}


def linear_annotations(sequence, labels):
    return {
        'molecule_id': 'lin',
        'topology': 'linear',
        'length': len(sequence),
        'sequence_sha256': hashlib.sha256(sequence.encode('ascii')).hexdigest(),
        'origin': 0,
        'labels': labels,
    }


class ProjectionTests(unittest.TestCase):
    def test_acceptance_r1_and_r2_projection(self):
        annotations = load(RING_ANNOTATIONS)
        r1 = build_release(load(R1_SOURCE), annotations, 'R1')
        r2 = build_release(load(R2_SOURCE), annotations, 'R2')

        self.assertEqual([f['fragment_id'] for f in r1['fragments']], ['ring:1'])
        self.assertEqual(r1['fragments'][0]['source_spans'], [[6, 8], [0, 6]])
        r1_mappings = r1['labels'][0]['mappings']
        self.assertEqual([m['source_span'] for m in r1_mappings],
                         [[5, 6], [6, 8], [0, 2]])
        self.assertEqual([m['label_span'] for m in r1_mappings],
                         [[0, 1], [1, 3], [3, 5]])
        self.assertEqual({m['fragment_id'] for m in r1_mappings}, {'ring:1'})
        self.assertEqual([m['fragment_span'] for m in r1_mappings],
                         [[7, 8], [0, 2], [2, 4]])

        self.assertEqual([f['fragment_id'] for f in r2['fragments']],
                         ['ring:1', 'ring:2'])
        r2_mappings = r2['labels'][0]['mappings']
        self.assertEqual([m['fragment_id'] for m in r2_mappings],
                         ['ring:1', 'ring:2', 'ring:1'])
        self.assertEqual([m['source_span'] for m in r2_mappings],
                         [[5, 6], [6, 8], [0, 2]])
        self.assertEqual([m['label_span'] for m in r2_mappings],
                         [[0, 1], [1, 3], [3, 5]])
        self.assertEqual([m['fragment_span'] for m in r2_mappings],
                         [[5, 6], [0, 2], [0, 2]])

    def test_release_carries_no_string_content_or_rules(self):
        release = build_release(load(R1_SOURCE), load(RING_ANNOTATIONS), 'R1')
        text = json.dumps(release)
        self.assertNotIn(RING_SEQUENCE, text)
        self.assertNotIn('CCGG', text)
        self.assertNotIn('motif', text)
        self.assertNotIn('enzyme', text)
        for fragment in release['fragments']:
            self.assertNotIn('sequence', fragment)
            self.assertNotIn('sha256', fragment)

    def test_fragments_without_labels_are_kept_in_order(self):
        annotations = ring_annotations([])
        release = build_release(load(R2_SOURCE), annotations, 'empty')
        self.assertEqual([f['fragment_id'] for f in release['fragments']],
                         ['ring:1', 'ring:2'])
        self.assertEqual(release['labels'], [])

    def test_labels_sorted_by_id_and_overlaps_allowed(self):
        annotations = ring_annotations([
            {'id': 'z-label', 'start': 0, 'length': 2},
            {'id': 'a-label', 'start': 0, 'length': 3},
        ])
        release = build_release(load(R1_SOURCE), annotations, 'overlap')
        self.assertEqual([label['id'] for label in release['labels']],
                         ['a-label', 'z-label'])

    def test_linear_head_and_tail_boundaries(self):
        sequence = 'ACGTACGT'
        source = linear_source(sequence)
        annotations = linear_annotations(sequence, [
            {'id': 'head', 'start': 0, 'length': 1},
            {'id': 'tail', 'start': 7, 'length': 1},
            {'id': 'full', 'start': 0, 'length': 8},
        ])
        release = build_release(source, annotations, 'lin-bound')
        report = process(source)
        cuts = report['cut_coordinates']
        self.assertEqual(cuts, [4])
        by_id = {label['id']: label['mappings'] for label in release['labels']}
        head = by_id['head'][0]
        self.assertEqual((head['source_span'], head['fragment_id'], head['fragment_span']),
                         ([0, 1], 'lin:1', [0, 1]))
        tail = by_id['tail'][0]
        self.assertEqual((tail['source_span'], tail['fragment_id'], tail['fragment_span']),
                         ([7, 8], 'lin:2', [3, 4]))
        full = by_id['full']
        self.assertEqual([m['label_span'] for m in full], [[0, 4], [4, 8]])
        self.assertEqual([m['source_span'] for m in full], [[0, 4], [4, 8]])
        self.assertEqual([m['fragment_span'] for m in full], [[0, 4], [0, 4]])

    def test_circular_full_turn_label_splits_at_source_zero(self):
        annotations = ring_annotations([{'id': 'turn', 'start': 6, 'length': 8}],
                                       origin=7)
        release = build_release(load(R2_SOURCE), annotations, 'full-turn')
        mappings = release['labels'][0]['mappings']
        self.assertEqual([m['label_span'] for m in mappings],
                         [[0, 1], [1, 3], [3, 8]])
        self.assertEqual([m['source_span'] for m in mappings],
                         [[5, 6], [6, 8], [0, 5]])
        self.assertEqual(sum(m['source_span'][1] - m['source_span'][0]
                             for m in mappings), 8)
        covered = []
        for mapping in mappings:
            lo, hi = mapping['source_span']
            covered.extend(range(lo, hi))
        self.assertEqual(sorted(covered), list(range(8)))
        self.assertEqual([m['fragment_id'] for m in mappings],
                         ['ring:1', 'ring:2', 'ring:1'])

    def test_full_turn_visiting_same_fragment_twice_is_not_merged(self):
        # 单切口环：片段 ring:1 含两个 source_span，整圈标注会两次访问同一片段。
        annotations = ring_annotations([{'id': 'turn', 'start': 0, 'length': 8}],
                                       origin=0)
        release = build_release(load(R1_SOURCE), annotations, 'twice')
        mappings = release['labels'][0]['mappings']
        self.assertEqual([m['source_span'] for m in mappings],
                         [[0, 6], [6, 8]])
        self.assertEqual([m['fragment_span'] for m in mappings],
                         [[2, 8], [0, 2]])
        self.assertTrue(all(m['fragment_id'] == 'ring:1' for m in mappings))

    def test_uncut_circle_full_turn_still_splits_at_zero(self):
        source = {
            'molecule': {'id': 'ring', 'topology': 'circular',
                         'sequence': RING_SEQUENCE},
            'enzymes': [{'id': 'none', 'motif': 'AAAA', 'cut_after': 1}],
        }
        annotations = ring_annotations([{'id': 'turn', 'start': 2, 'length': 8}])
        release = build_release(source, annotations, 'uncut-turn')
        fragment = release['fragments'][0]
        self.assertTrue(fragment['circular'])
        self.assertEqual(fragment['source_spans'], [[0, 8]])
        mappings = release['labels'][0]['mappings']
        self.assertEqual([m['source_span'] for m in mappings],
                         [[1, 8], [0, 1]])
        self.assertEqual([m['label_span'] for m in mappings],
                         [[0, 7], [7, 8]])

    def test_crossing_external_zero_without_source_zero_keeps_one_record(self):
        # origin=2：外部零点对应源位置 2。未切环只有一个 span [0,8)。
        # 外部 start=7, length=4 → 源走向 1,2,3,4，跨过外部零点（源2）但不跨源零点，
        # 按契约外部过 0 不额外拆记录，应只有一条等长记录。
        source = {
            'molecule': {'id': 'ring', 'topology': 'circular',
                         'sequence': RING_SEQUENCE},
            'enzymes': [{'id': 'none', 'motif': 'AAAA', 'cut_after': 1}],
        }
        annotations = ring_annotations([{'id': 'cross', 'start': 7, 'length': 4}],
                                       origin=2)
        release = build_release(source, annotations, 'ext-zero')
        mappings = release['labels'][0]['mappings']
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0]['label_span'], [0, 4])
        self.assertEqual(mappings[0]['source_span'], [1, 5])
        self.assertEqual(mappings[0]['fragment_span'], [1, 5])

    def test_origin_shift_without_wrap_offsets_source_coordinates(self):
        annotations = ring_annotations([{'id': 'plain', 'start': 0, 'length': 3}],
                                       origin=3)
        release = build_release(load(R2_SOURCE), annotations, 'shift')
        mappings = release['labels'][0]['mappings']
        self.assertEqual([m['source_span'] for m in mappings], [[3, 6]])
        self.assertEqual([m['label_span'] for m in mappings], [[0, 3]])
        self.assertEqual(mappings[0]['fragment_id'], 'ring:1')
        self.assertEqual(mappings[0]['fragment_span'], [3, 6])

    def test_wrong_source_hash_rejected_even_when_identity_matches(self):
        annotations = load(RING_ANNOTATIONS)
        annotations['sequence_sha256'] = '0' * 64
        with self.assertRaises(LineageError):
            build_release(load(R1_SOURCE), annotations, 'wrong-hash')

    def test_source_identity_topology_and_length_must_match(self):
        base = load(RING_ANNOTATIONS)
        for field_name, wrong in (
            ('molecule_id', 'other'),
            ('topology', 'linear'),
            ('length', 7),
        ):
            annotations = dict(base)
            annotations[field_name] = wrong
            if field_name == 'topology':
                annotations['origin'] = 0
            with self.assertRaises(LineageError):
                build_release(load(R1_SOURCE), annotations, 'mismatch')

    def test_annotation_validation_rules(self):
        good = {'id': 'x', 'start': 0, 'length': 1}
        cases = [
            ('bad-id', dict(labels=[dict(good, id='')]), 'circular', 7),
            ('bad-start', dict(labels=[dict(good, start=-1)]), 'circular', 7),
            ('start-out', dict(labels=[dict(good, start=8)]), 'circular', 7),
            ('zero-length', dict(labels=[dict(good, length=0)]), 'circular', 7),
            ('over-turn', dict(labels=[dict(good, length=9)]), 'circular', 7),
            ('linear-overrun', dict(labels=[{'id': 'x', 'start': 7, 'length': 2}]),
             'linear', 0),
            ('linear-origin', dict(labels=[good]), 'linear', 1),
            ('origin-out', dict(labels=[good]), 'circular', 8),
            ('uppercase-hash', dict(labels=[good],
                                    sequence_sha256=RING_SHA256.upper()),
             'circular', 7),
            ('nonhex-hash', dict(labels=[good], sequence_sha256='z' * 64),
             'circular', 7),
            ('duplicate-label', dict(labels=[good, good]), 'circular', 7),
        ]
        for _name, overrides, topology, origin in cases:
            labels = overrides.pop('labels')
            annotations = ring_annotations(labels, origin=origin)
            annotations['topology'] = topology
            annotations.update(overrides)
            with self.assertRaises((LineageError, ValueError)):
                build_release(load(R1_SOURCE), annotations, 'bad')

    def test_unknown_top_level_field_rejected(self):
        annotations = ring_annotations([{'id': 'x', 'start': 0, 'length': 1}])
        annotations['extra'] = 1
        with self.assertRaises(ValueError):
            build_release(load(R1_SOURCE), annotations, 'extra-field')

    def test_boolean_is_not_accepted_as_integer(self):
        annotations = ring_annotations([{'id': 'x', 'start': 0, 'length': True}])
        with self.assertRaises(ValueError):
            build_release(load(R1_SOURCE), annotations, 'bool')

    def test_duplicate_json_keys_rejected(self):
        raw = Path(RING_ANNOTATIONS).read_text(encoding='utf-8').replace(
            '"origin": 7', '"origin": 7, "origin": 0')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'dup.json'
            path.write_text(raw, encoding='utf-8')
            with self.assertRaises(ValueError):
                build_release(load(R1_SOURCE), load(path), 'dup')

    def test_label_limits_enforced(self):
        annotations = ring_annotations(
            [{'id': f'l{i}', 'start': 0, 'length': 1} for i in range(1001)])
        with self.assertRaises(LineageError):
            build_release(load(R1_SOURCE), annotations, 'too-many')
        annotations = ring_annotations([{'id': 'big', 'start': 0, 'length': 200001}])
        with self.assertRaises(LineageError):
            build_release(load(R1_SOURCE), annotations, 'too-long')
        annotations = ring_annotations(
            [{'id': f'l{i}', 'start': 0, 'length': 200} for i in range(1000)])
        with self.assertRaises(LineageError):
            build_release(load(R1_SOURCE), annotations, 'total-too-long')


class SealExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.archive = self.work / 'archive'
        self.receipts = self.work / 'receipts'
        self.exports = self.work / 'exports'

    def tearDown(self):
        self.tmp.cleanup()

    def seal(self, source, release_id):
        payload, receipt = seal(source, RING_ANNOTATIONS, self.archive, release_id)
        receipt_path = self.receipts / f'{release_id}.json'
        write_json(receipt_path, receipt)
        return payload, receipt, receipt_path

    def test_seal_receipt_matches_canonical_export_bytes(self):
        payload, receipt, receipt_path = self.seal(R1_SOURCE, 'R1')
        self.assertEqual(set(receipt), {'release_id', 'export_sha256'})
        self.assertEqual(receipt['release_id'], 'R1')
        self.assertEqual(receipt['export_sha256'],
                         hashlib.sha256(payload).hexdigest())
        output = self.exports / 'r1.json'
        exported = export(self.archive, receipt_path, output)
        self.assertEqual(exported, payload)
        self.assertEqual(output.read_bytes(), payload)

    def test_seal_is_idempotent_and_rebind_is_rejected(self):
        _, receipt1, receipt_path = self.seal(R1_SOURCE, 'R1')
        _, receipt2, _ = self.seal(R1_SOURCE, 'R1')
        self.assertEqual(receipt1, receipt2)
        with self.assertRaises(LineageError):
            seal(R2_SOURCE, RING_ANNOTATIONS, self.archive, 'R1')
        # 失败的改绑没有破坏旧发布，原回执仍可重发。
        output = self.exports / 'still-r1.json'
        export(self.archive, receipt_path, output)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(),
                         receipt1['export_sha256'])
        release = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(len(release['fragments']), 1)

    def test_full_acceptance_relocate_archive_and_resend_from_new_process(self):
        payload_r1, receipt_r1, receipt_r1_path = self.seal(R1_SOURCE, 'R1')
        payload_r2, receipt_r2, receipt_r2_path = self.seal(R2_SOURCE, 'R2')
        self.assertNotEqual(receipt_r1['export_sha256'], receipt_r2['export_sha256'])

        # 模拟移走全部输入与已导出的发布文件。
        moved_archive = self.work / 'moved' / 'archive-elsewhere'
        moved_archive.parent.mkdir(parents=True)
        shutil.move(str(self.archive), str(moved_archive))
        moved_receipts = self.work / 'moved' / 'receipts'
        shutil.move(str(self.receipts), str(moved_receipts))

        out_dir = self.work / 'moved' / 'out'
        out_dir.mkdir(parents=True)
        r1_out = out_dir / 'r1-release.json'
        r2_out = out_dir / 'r2-release.json'
        r1_receipt = moved_receipts / 'R1.json'
        r2_receipt = moved_receipts / 'R2.json'

        result = run_cli('export', '--archive', str(moved_archive),
                         '--receipt', str(r1_receipt), '--output', str(r1_out),
                         cwd=str(self.work))
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run_cli('export', '--archive', str(moved_archive),
                         '--receipt', str(r2_receipt), '--output', str(r2_out),
                         cwd=str(self.work))
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(r1_out.read_bytes(), payload_r1)
        self.assertEqual(r2_out.read_bytes(), payload_r2)
        self.assertEqual(hashlib.sha256(r1_out.read_bytes()).hexdigest(),
                         receipt_r1['export_sha256'])
        self.assertEqual(hashlib.sha256(r2_out.read_bytes()).hexdigest(),
                         receipt_r2['export_sha256'])

        r1_release = json.loads(r1_out.read_text(encoding='utf-8'))
        r2_release = json.loads(r2_out.read_text(encoding='utf-8'))
        self.assertEqual(len(r1_release['fragments']), 1)
        self.assertEqual(len(r2_release['fragments']), 2)
        self.assertEqual([m['fragment_id'] for m in
                          r1_release['labels'][0]['mappings']],
                         ['ring:1', 'ring:1', 'ring:1'])
        self.assertEqual([m['fragment_id'] for m in
                          r2_release['labels'][0]['mappings']],
                         ['ring:1', 'ring:2', 'ring:1'])
        # 归档内容未被 export 改动。
        self.assertEqual(sorted(p.name for p in moved_archive.iterdir()),
                         ['R1', 'R2'])

    def test_wrong_receipt_hash_and_corrupt_archive_produce_no_file(self):
        payload_r1, receipt_r1, receipt_r1_path = self.seal(R1_SOURCE, 'R1')
        self.seal(R2_SOURCE, 'R2')
        output = self.exports / 'must-not-exist.json'

        tampered = self.work / 'tampered-receipt.json'
        bad_receipt = dict(receipt_r1)
        bad_receipt['export_sha256'] = self.seal(R2_SOURCE, 'R2')[1]['export_sha256']
        write_json(tampered, bad_receipt)
        with self.assertRaises(LineageError):
            export(self.archive, tampered, output)
        self.assertFalse(output.exists())

        # 损坏归档副本中 R1 重发所需的数据。
        corrupt_archive = self.work / 'corrupt-archive'
        shutil.copytree(self.archive, corrupt_archive)
        release_file = corrupt_archive / 'R1' / 'release.json'
        raw = bytearray(release_file.read_bytes())
        raw[10] = raw[10] ^ 0x01
        release_file.write_bytes(bytes(raw))
        with self.assertRaises(LineageError):
            export(corrupt_archive, receipt_r1_path, output)
        self.assertFalse(output.exists())

        # 缺发布。
        missing_archive = self.work / 'missing-archive'
        shutil.copytree(self.archive, missing_archive)
        shutil.rmtree(missing_archive / 'R1')
        with self.assertRaises(LineageError):
            export(missing_archive, receipt_r1_path, output)
        self.assertFalse(output.exists())

        # 完好归档上的正确回执仍可重发。
        good_output = self.exports / 'good.json'
        good_payload = export(self.archive, receipt_r1_path, good_output)
        self.assertEqual(good_payload, payload_r1)

    def test_export_refuses_existing_target_including_symlink(self):
        _, _, receipt_path = self.seal(R1_SOURCE, 'R1')
        existing = self.exports / 'existing.json'
        write_json(existing, {})
        with self.assertRaises(LineageError):
            export(self.archive, receipt_path, existing)
        self.assertEqual(existing.read_text(encoding='utf-8'), '{}\n')

        link = self.exports / 'link.json'
        link.symlink_to(existing)
        with self.assertRaises(LineageError):
            export(self.archive, receipt_path, link)
        self.assertTrue(link.is_symlink())

    def test_invalid_input_leaves_no_half_sealed_release(self):
        bad_annotations = self.work / 'bad-annotations.json'
        bad = ring_annotations([{'id': 'x', 'start': 0, 'length': 9}])
        write_json(bad_annotations, bad)
        with self.assertRaises(LineageError):
            seal(R1_SOURCE, bad_annotations, self.archive, 'ghost')
        self.assertFalse((self.archive / 'ghost').exists())
        self.assertEqual(list(self.archive.glob('.staging-*')), [])
        self.assertEqual(list(self.archive.iterdir()), [])

    def test_seal_refuses_symlinked_release_path(self):
        outside = self.work / 'outside'
        outside.mkdir()
        self.archive.mkdir(parents=True)
        (self.archive / 'R1').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(LineageError):
            seal(R1_SOURCE, RING_ANNOTATIONS, self.archive, 'R1')
        self.assertTrue((self.archive / 'R1').is_symlink())
        self.assertEqual(list(outside.iterdir()), [])

    def test_simulated_write_failure_then_retry_succeeds_with_same_id(self):
        calls = {'count': 0}

        def fail_once(staging):
            calls['count'] += 1
            raise OSError('simulated write failure')

        with self.assertRaises(OSError):
            seal(R1_SOURCE, RING_ANNOTATIONS, self.archive, 'R1',
                 fail_writer=fail_once)
        # 半成品已清理，ID 未被占住。
        self.assertFalse((self.archive / 'R1').exists())
        self.assertEqual(list(self.archive.glob('.staging-*')), [])

        payload, receipt, _ = self.seal(R1_SOURCE, 'R1')
        self.assertEqual(calls['count'], 1)
        self.assertEqual(receipt['export_sha256'],
                         hashlib.sha256(payload).hexdigest())
        receipt_path = self.receipts / 'R1.json'
        output = self.exports / 'retry.json'
        export(self.archive, receipt_path, output)
        self.assertEqual(output.read_bytes(), payload)

    def test_stale_staging_from_killed_process_is_reclaimed(self):
        payload, receipt, receipt_path = self.seal(R1_SOURCE, 'R1')
        stale = self.archive / '.staging-dead-process'
        stale.mkdir()
        (stale / 'release.json').write_bytes(b'partial')
        _, receipt_again, _ = self.seal(R1_SOURCE, 'R1')
        self.assertEqual(receipt_again, receipt)
        self.assertFalse(stale.exists())
        output = self.exports / 'after-stale.json'
        self.assertEqual(export(self.archive, receipt_path, output), payload)

    def test_seal_cli_emits_one_line_receipt_and_export_cli_roundtrip(self):
        result = run_cli('seal', '--source', str(R1_SOURCE),
                         '--annotations', str(RING_ANNOTATIONS),
                         '--archive', str(self.archive), '--release-id', 'CLI1')
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(set(receipt), {'release_id', 'export_sha256'})
        self.assertEqual(result.stdout.count('\n'), 1)
        receipt_path = self.work / 'cli-receipt.json'
        write_json(receipt_path, receipt)
        output = self.exports / 'cli.json'
        result = run_cli('export', '--archive', str(self.archive),
                         '--receipt', str(receipt_path), '--output', str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(),
                         receipt['export_sha256'])

    def test_seal_cli_rebind_failure_exits_2(self):
        run_cli('seal', '--source', str(R1_SOURCE),
                '--annotations', str(RING_ANNOTATIONS),
                '--archive', str(self.archive), '--release-id', 'R1')
        result = run_cli('seal', '--source', str(R2_SOURCE),
                         '--annotations', str(RING_ANNOTATIONS),
                         '--archive', str(self.archive), '--release-id', 'R1')
        self.assertEqual(result.returncode, 2)
        self.assertIn('bound to different content', result.stderr)

    def test_export_cli_missing_receipt_exits_2(self):
        result = run_cli('export', '--archive', str(self.archive),
                         '--receipt', str(self.work / 'nope.json'),
                         '--output', str(self.exports / 'x.json'))
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.exports / 'x.json').exists())

    def test_serialization_is_stable_across_paths_and_processes(self):
        payload, receipt, receipt_path = self.seal(R1_SOURCE, 'R1')
        other_archive = self.work / 'other' / 'deep' / 'archive'
        other_archive.parent.mkdir(parents=True)
        shutil.move(str(self.archive), str(other_archive))
        output = self.exports / 'stable.json'
        result = run_cli('export', '--archive', str(other_archive),
                         '--receipt', str(receipt_path), '--output', str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_bytes(), payload)
        self.assertEqual(canonical_bytes(json.loads(payload.decode('utf-8'))),
                         payload)

    def test_receipt_validation(self):
        self.seal(R1_SOURCE, 'R1')
        output = self.exports / 'x.json'
        for bad in (
            {'release_id': 'R1'},
            {'export_sha256': '0' * 64},
            {'release_id': 'R1', 'export_sha256': '0' * 63},
            {'release_id': 'R1', 'export_sha256': ('A' * 64)},
            {'release_id': '../escape', 'export_sha256': '0' * 64},
            {'release_id': 'R1', 'export_sha256': '0' * 64, 'extra': 1},
        ):
            receipt_path = self.work / 'bad-receipt.json'
            write_json(receipt_path, bad)
            with self.assertRaises((LineageError, ValueError)):
                export(self.archive, receipt_path, output)
            self.assertFalse(output.exists())


class LegacyEntryTests(unittest.TestCase):
    """旧入口 sequence-digest 的报告与错误行为不回归。"""

    def test_v1_report_unchanged(self):
        report = process(load(V1_SOURCE))
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['cut_coordinates'], [0, 6])
        self.assertEqual([f['sequence'] for f in report['fragments']],
                         ['AATTCC', 'GG'])
        self.assertEqual(report['summary'], {
            'bases': 8, 'recognition_hits': 2, 'unique_cuts': 2,
            'fragments': 2, 'fragment_bases': 8,
        })

    def test_legacy_cli_still_works_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'report.json'
            env = dict(os.environ)
            env['PYTHONPATH'] = str(SRC)
            result = subprocess.run(
                [sys.executable, '-P', '-m', 'sequence_digest.cli',
                 '--source', str(V1_SOURCE), '--output', str(output)],
                env=env, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 0, result.stderr)
            first = output.read_bytes()
            result = subprocess.run(
                [sys.executable, '-P', '-m', 'sequence_digest.cli',
                 '--source', str(V1_SOURCE), '--output', str(output)],
                env=env, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(output.read_bytes(), first)

    def test_legacy_invalid_input_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / 'bad.json'
            bad.write_text('{"molecule": {}', encoding='utf-8')
            env = dict(os.environ)
            env['PYTHONPATH'] = str(SRC)
            result = subprocess.run(
                [sys.executable, '-P', '-m', 'sequence_digest.cli',
                 '--source', str(bad), '--output', str(Path(tmp) / 'o.json')],
                env=env, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 2)
            self.assertFalse((Path(tmp) / 'o.json').exists())


if __name__ == '__main__':
    unittest.main()

import json
from pathlib import Path
import unittest
from sequence_digest.service import process
from sequence_digest.recognize import reverse_complement

def sample():return json.loads((Path(__file__).resolve().parents[1]/'fixtures/input.json').read_text())

class DigestTests(unittest.TestCase):
    def test_circular_origin_and_fragment_sequence(self):
        r=process(sample())
        self.assertEqual(r['cut_coordinates'],[0,6])
        self.assertEqual([f['sequence'] for f in r['fragments']],['AATTCC','GG'])
        hit=next(h for h in r['hits'] if h['enzyme_id']=='demo-palindrome')
        self.assertEqual((hit['start'],hit['wraps_origin'],hit['orientation']),(7,True,'+'))
        self.assertEqual(r['summary']['fragment_bases'],8)

    def test_reverse_orientation_maps_cut_offset(self):
        d=sample();d['molecule'].update(topology='linear',sequence='TTGACTAA');d['enzymes']=[dict(id='reverse',motif='AGTC',cut_after=1)]
        r=process(d)
        self.assertEqual(r['hits'][0]['orientation'],'-');self.assertEqual(r['cut_coordinates'],[5])
        self.assertEqual([f['sequence'] for f in r['fragments']],['TTGAC','TAA'])

    def test_coincident_hits_do_not_create_zero_length_fragments(self):
        d=sample();d['enzymes'].append(dict(id='copy',motif='CCGG',cut_after=2))
        r=process(d)
        self.assertEqual(r['summary']['recognition_hits'],3);self.assertEqual(r['summary']['unique_cuts'],2)
        self.assertTrue(all(f['length']>0 for f in r['fragments']))

    def test_uncut_circle_and_single_cut_rotation(self):
        d=sample();d['enzymes']=[dict(id='none',motif='AAAA',cut_after=1)]
        r=process(d);self.assertTrue(r['fragments'][0]['circular']);self.assertEqual(r['unmatched_enzyme_ids'],['none'])
        d['enzymes']=[dict(id='one',motif='CCGG',cut_after=2)]
        f=process(d)['fragments'][0]
        self.assertFalse(f['circular']);self.assertEqual(f['sequence'],'GGAATTCC');self.assertEqual(f['source_spans'],[[6,8],[0,6]])

    def test_linear_end_cut_not_empty_fragment(self):
        d=sample();d['molecule'].update(topology='linear',sequence='GAATTC');d['enzymes']=[dict(id='end',motif='GAATTC',cut_after=0)]
        r=process(d);self.assertEqual(r['cut_coordinates'],[0]);self.assertEqual(len(r['fragments']),1)
        self.assertEqual(reverse_complement('AAGC'),'GCTT')

    def test_invalid_alphabet_offset_and_duplicate(self):
        d=sample();d['molecule']['sequence']='ACNT'
        with self.assertRaises(ValueError):process(d)
        d=sample();d['enzymes'][0]['cut_after']=True
        with self.assertRaises(ValueError):process(d)
        d=sample();d['enzymes'][1]['id']=d['enzymes'][0]['id']
        with self.assertRaises(ValueError):process(d)

if __name__=='__main__':unittest.main()

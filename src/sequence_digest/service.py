from .fragments import split
from .model import validate
from .recognize import recognize

def process(data):
    molecule,enzymes=validate(data);hits=recognize(molecule,enzymes)
    cuts=sorted({h['cut'] for h in hits});fragments=split(molecule,cuts)
    matched={h['enzyme_id'] for h in hits}
    return {'schema_version':1,'molecule_id':molecule['id'],'topology':molecule['topology'],'hits':hits,'cut_coordinates':cuts,'fragments':fragments,
            'unmatched_enzyme_ids':sorted(e['id'] for e in enzymes if e['id'] not in matched),
            'summary':{'bases':len(molecule['sequence']),'recognition_hits':len(hits),'unique_cuts':len(cuts),'fragments':len(fragments),'fragment_bases':sum(f['length'] for f in fragments)}}

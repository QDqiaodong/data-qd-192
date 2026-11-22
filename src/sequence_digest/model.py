import re
from .checks import array,fields,ident,integer,unique

def dna(value,lo,hi):
    if not isinstance(value,str) or not lo<=len(value)<=hi or re.fullmatch('[ACGT]+',value) is None:raise ValueError('uppercase ACGT sequence required')
    return value

def validate(data):
    fields(data,('molecule','enzymes'))
    molecule=fields(data['molecule'],('id','topology','sequence'));ident(molecule['id'])
    if molecule['topology'] not in ('linear','circular'):raise ValueError('linear/circular topology required')
    sequence=dna(molecule['sequence'],1,200000)
    enzymes=array(data['enzymes'],1,50)
    if len(sequence)*sum(len(e.get('motif','')) if isinstance(e,dict) and isinstance(e.get('motif'),str) else 1 for e in enzymes)>10000000:
        raise ValueError('matching work bound exceeded')
    for e in enzymes:
        fields(e,('id','motif','cut_after'));ident(e['id'])
        motif=dna(e['motif'],1,32)
        if len(motif)>len(sequence):raise ValueError('motif longer than molecule')
        integer(e['cut_after'],0,len(motif))
    unique([e['id'] for e in enzymes])
    return molecule,enzymes

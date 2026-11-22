import hashlib

def split(molecule,cuts):
    sequence=molecule['sequence'];n=len(sequence);circular=molecule['topology']=='circular'
    if not circular:
        points=sorted({0,n,*cuts});arcs=[(a,b-a,False) for a,b in zip(points,points[1:])]
    elif not cuts:
        arcs=[(0,n,True)]
    else:
        points=sorted(cuts)
        arcs=[(a,(points[(i+1)%len(points)]-a)%n or n,False) for i,a in enumerate(points)]
    result=[]
    for start,length,is_circle in arcs:
        remaining=n-start
        bases=(sequence[start:]+sequence[:max(0,length-remaining)])[:length]
        spans=[[start,start+length]] if start+length<=n else [[start,n],[0,start+length-n]]
        result.append({'fragment_id':f'{molecule["id"]}:{len(result)+1}','start':start,'length':length,'circular':is_circle,'source_spans':spans,
                       'sequence':bases,'sha256':hashlib.sha256(bases.encode('ascii')).hexdigest()})
    if sum(f['length'] for f in result)!=n:raise ValueError('internal sequence conservation failure')
    return result

def reverse_complement(sequence):return sequence.translate(str.maketrans('ACGT','TGCA'))[::-1]

def recognize(molecule,enzymes):
    sequence=molecule['sequence'];n=len(sequence);circular=molecule['topology']=='circular';hits=[]
    for enzyme in sorted(enzymes,key=lambda e:e['id']):
        motif=enzyme['motif'];m=len(motif);reverse=reverse_complement(motif)
        patterns=[('+',motif,enzyme['cut_after'])]
        if reverse!=motif:patterns.append(('-',reverse,m-enzyme['cut_after']))
        search=sequence+sequence[:m-1] if circular else sequence
        limit=n if circular else n-m+1
        for orientation,pattern,offset in patterns:
            for start in range(limit):
                if search.startswith(pattern,start):
                    cut=start+offset
                    hits.append({'enzyme_id':enzyme['id'],'start':start,'orientation':orientation,'motif_length':m,
                                 'wraps_origin':start+m>n,'cut':cut%n if circular else cut})
    return sorted(hits,key=lambda h:(h['cut'],h['enzyme_id'],h['start'],h['orientation']))

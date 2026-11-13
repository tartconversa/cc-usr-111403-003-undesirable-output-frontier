"""现有业务摘要，不推断未观察到的业务关系。"""
def analyze(snapshot):
    totals={k:[0]*n for k,n in snapshot.metadata['dimensions'].items()}
    for r in snapshot.rows('firms'):
        for k,n in snapshot.metadata['dimensions'].items():
            if len(r[k])!=n or any(v<0 for v in r[k]):raise ValueError('投入产出维数或数值无效')
            totals[k]=[a+b for a,b in zip(totals[k],r[k])]
    return totals

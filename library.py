import csv,io,json,re
from smart import norm,metadata

def parse_content(raw,fmt):
 if fmt=='json': rows=json.loads(raw)
 elif fmt=='csv': rows=list(csv.DictReader(io.StringIO(raw)))
 else: rows=[{'title':x.strip()} for x in raw.splitlines() if x.strip()]
 if not isinstance(rows,list): raise ValueError('Expected a list')
 out={}
 for row in rows:
  if isinstance(row,str): row={'title':row}
  if not isinstance(row,dict): raise ValueError('Invalid content row')
  t=str(row.get('title','')).strip(); d=str(row.get('description','') or '').strip(); h=str(row.get('hashtags','') or '').strip(); k=str(row.get('keywords','') or '').strip()
  if not norm(t) or len(t)>99 or len(d)>4500 or len(h)>400 or len(k)>1000: raise ValueError('Invalid title or metadata length: '+t[:50])
  out[norm(t)]=(t,d,h,k)
 return out

def choose(topic,names,used,extra,rows,hashtags,keywords,mode):
 seen=set(used); out=[]
 if mode!='internal':
  for r in rows:
   if norm(r['title']) in seen: continue
   seen.add(norm(r['title'])); out.append({'title':r['title'],'description':r['description'] or r['title']+'. A reflection on '+topic+'.','hashtags':r['hashtags'] or hashtags,'keywords':r['keywords'] or keywords})
   if len(out)==len(names): break
 if len(out)<len(names):
  if mode=='library': raise ValueError('Not enough unused library titles. Import more or select Hybrid.')
  for r in metadata(topic,names[len(out):],used=seen,extra=extra):
   r['hashtags']=hashtags; r['keywords']=keywords; out.append(r)
 for r in out:
  if r['hashtags']: r['description']+='\n\n'+r['hashtags']
  if r['keywords']: r['description']+='\n\nKeywords: '+r['keywords']
  r['description']=r['description'][:5000]
 return out

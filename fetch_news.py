import os,json,re,hashlib,time
from datetime import datetime,timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parent; DATA=ROOT/'data'; ARCHIVE=DATA/'archive'; DATA.mkdir(exist_ok=True); ARCHIVE.mkdir(exist_ok=True)
OUT=DATA/'news.json'; NOW=datetime.now(timezone.utc); H={'User-Agent':'Forschungsmonitor/1.0'}
U=['"Usher 1B" OR USH1B OR MYO7A','"LUCE-1" OR "AAVB-081"','"MYO7A" "gene therapy"','"Usher syndrome" gene therapy']
D=['"type 2 diabetes" new treatment','"type 2 diabetes" clinical trial','"type 2 diabetes" GLP-1 GIP']

def clean(x): return re.sub(r'\s+',' ',BeautifulSoup(str(x or ''),'html.parser').get_text(' ',strip=True)).strip()
def gid(t,u): return hashlib.sha256((t+'|'+u).encode()).hexdigest()[:20]
def gdelt(q):
 try:return requests.get('https://api.gdeltproject.org/api/v2/doc/doc',params={'query':q,'mode':'artlist','format':'json','maxrecords':25,'timespan':'14days','sort':'datedesc'},headers=H,timeout=30).json().get('articles',[])
 except Exception as e: print('GDELT:',e); return []
def pubmed(q):
 try:
  b='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'; ids=requests.get(b+'esearch.fcgi',params={'db':'pubmed','term':q,'retmode':'json','retmax':15,'sort':'pub date'},headers=H,timeout=30).json()['esearchresult']['idlist']
  if not ids:return []
  d=requests.get(b+'esummary.fcgi',params={'db':'pubmed','id':','.join(ids),'retmode':'json'},headers=H,timeout=30).json()['result']
  return [{'title':d[i].get('title',''),'url':f'https://pubmed.ncbi.nlm.nih.gov/{i}/','source':'PubMed','published':d[i].get('pubdate','')} for i in ids]
 except Exception as e: print('PubMed:',e); return []
def trials(q):
 try:
  data=requests.get('https://clinicaltrials.gov/api/v2/studies',params={'query.term':q,'pageSize':20},headers=H,timeout=30).json(); out=[]
  for s in data.get('studies',[]):
   p=s.get('protocolSection',{}); i=p.get('identificationModule',{}); st=p.get('statusModule',{}); n=i.get('nctId','')
   out.append({'title':f"{i.get('briefTitle','')} ({st.get('overallStatus','')})",'url':f'https://clinicaltrials.gov/study/{n}','source':'ClinicalTrials.gov','published':st.get('studyFirstPostDateStruct',{}).get('date','')})
  return out
 except Exception as e: print('ClinicalTrials:',e); return []
def text(url):
 try:
  s=BeautifulSoup(requests.get(url,headers=H,timeout=35).text,'html.parser')
  for x in s(['script','style','nav','footer','header','aside','form']):x.decompose()
  m=s.find('article') or s.find('main') or s.body
  return clean(m.get_text(' ',strip=True) if m else '')[:16000]
 except Exception as e: print('Artikel:',e); return ''
def ai(title,source,url,body):
 token=os.getenv('GITHUB_TOKEN')
 if not token or not body:return {}
 prompt=f'''Du bist wissenschaftlicher Redakteur für einen deutschen Forschungsmonitor.
Quelle: {source}\nOriginaltitel: {title}\nURL: {url}\n\nOriginaltext:\n{body}\n\nErstelle ausschließlich auf Basis des Textes: title_de (präzise deutsche Überschrift), summary_de (kurze deutsche Zusammenfassung), detailed_summary_de (ausführliche deutsche Zusammenfassung mit Studiendesign, Patientenzahl, Status, Ergebnissen, Sicherheit, Wirksamkeit und Einschränkungen, sofern vorhanden), why_relevant (Relevanz für Usher 1B/MYO7A/LUCE-1/AAVB-081 oder Typ-2-Diabetes). Nichts erfinden. Antworte nur als JSON mit diesen vier Feldern.'''
 try:
  r=requests.post('https://models.github.ai/inference/chat/completions',headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'},json={'model':'openai/gpt-4.1-mini','messages':[{'role':'system','content':'Du schreibst sorgfältig und verständlich auf Deutsch.'},{'role':'user','content':prompt}],'temperature':0.1,'max_tokens':3500},timeout=120); r.raise_for_status(); c=r.json()['choices'][0]['message']['content'].strip(); c=re.sub(r'^```json\s*|\s*```$','',c,flags=re.I); return json.loads(c)
 except Exception as e: print('GitHub Models:',e); return {}
def topic(t):
 s=t.lower()
 if any(x in s for x in ['usher','ush1b','myo7a','luce-1','aavb-081']):return 'usher'
 if any(x in s for x in ['diabetes','glp-1','gip','glucose','cgm']):return 'diabetes'
 return ''
def prio(t):
 s=t.lower(); return sum(p for w,p in [('luce-1',100),('aavb-081',100),('usher 1b',95),('ush1b',95),('myo7a',90),('gene therapy',70),('clinical trial',65),('type 2 diabetes',50),('glp-1',45),('gip',45)] if w in s)
def main():
 raw=[]
 for q in U+D: raw+=gdelt(q)
 for q in U+D: raw+=pubmed(q)
 raw+=trials('Usher syndrome type 1B MYO7A'); raw+=trials('type 2 diabetes')
 seen=set(); cand=[]
 for x in raw:
  t=clean(x.get('title','')); u=x.get('url','')
  if t and u and u not in seen and topic(t): seen.add(u); x['title']=t; x['topic']=topic(t); cand.append(x)
 cand.sort(key=lambda x:prio(x['title']),reverse=True); items=[]
 for x in cand[:30]:
  t,u=x['title'],x['url']; src=x.get('source') or x.get('domain') or 'Unbekannte Quelle'; a=ai(t,src,u,text(u)); td=clean(a.get('title_de','')) or t; sm=clean(a.get('summary_de','')) or 'Keine deutsche Kurzfassung verfügbar.'; ds=clean(a.get('detailed_summary_de','')) or sm; wr=clean(a.get('why_relevant','')) or 'Thematisch relevante Meldung.'
  items.append({'id':gid(t,u),'title':t,'title_de':td,'summary_de':sm,'detailed_summary_de':ds,'why_relevant':wr,'url':u,'source':src,'published':x.get('published',''),'topic':x['topic'],'priority':prio(t)}); time.sleep(.5)
 result={'schema_version':'4.0','generated_at':NOW.isoformat(),'generated_at_display':NOW.strftime('%d.%m.%Y %H:%M UTC'),'sources_checked':len(U)+len(D),'items':items}
 OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8'); (ARCHIVE/NOW.strftime('%Y-%m-%d.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8'); print('Fertig:',len(items),'Meldungen')
if __name__=='__main__':main()

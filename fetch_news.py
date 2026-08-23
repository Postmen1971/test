import os,json,re,hashlib,time
from datetime import datetime,timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'data'; ARCHIVE=DATA/'archive'
DATA.mkdir(exist_ok=True); ARCHIVE.mkdir(exist_ok=True)
OUT=DATA/'news.json'
NOW=datetime.now(timezone.utc)

H={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36 Forschungsmonitor/2.0','Accept-Language':'de-DE,de;q=0.9,en;q=0.8'}
CONFIG=json.loads((ROOT/'config.json').read_text(encoding='utf-8')) if (ROOT/'config.json').exists() else {}
PRIORITY_TERMS=[t.lower() for t in (CONFIG.get('usher',{}).get('priority',[])+CONFIG.get('diabetes',{}).get('priority',[]))]

def clean(x): return re.sub(r'\s+',' ',BeautifulSoup(str(x or ''),'html.parser').get_text(' ',strip=True)).strip()
def gid(t,u): return hashlib.sha256((t+'|'+u).encode()).hexdigest()[:20]

def gdelt(q):
    try:
        r=requests.get('https://api.gdeltproject.org/api/v2/doc/doc',params={'query':q,'mode':'artlist','format':'json','maxrecords':25,'timespan':'14days','sort':'datedesc'},headers=H,timeout=30); r.raise_for_status(); return r.json().get('articles',[])
    except Exception as e: print('GDELT-Fehler:',e); return []

def pubmed_search(q):
    try:
        b='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
        ids=requests.get(b+'esearch.fcgi',params={'db':'pubmed','term':q,'retmode':'json','retmax':15,'sort':'pub date'},headers=H,timeout=30).json()['esearchresult']['idlist']
        if not ids:return []
        d=requests.get(b+'esummary.fcgi',params={'db':'pubmed','id':','.join(ids),'retmode':'json'},headers=H,timeout=30).json()['result']
        return [{'title':d[i].get('title',''),'url':f'https://pubmed.ncbi.nlm.nih.gov/{i}/','source':'PubMed','published':d[i].get('pubdate',''),'pmid':i} for i in ids]
    except Exception as e: print('PubMed-Fehler:',e); return []

def pubmed_abstract(pmid):
    if not pmid:return ''
    try:
        r=requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi',params={'db':'pubmed','id':pmid,'rettype':'abstract','retmode':'text'},headers=H,timeout=30); r.raise_for_status(); return clean(r.text)[:10000]
    except Exception as e: print('PubMed-Abstract-Fehler:',e); return ''

def trials(q):
    try:
        r=requests.get('https://clinicaltrials.gov/api/v2/studies',params={'query.term':q,'pageSize':20},headers=H,timeout=30); r.raise_for_status(); out=[]
        for s in r.json().get('studies',[]):
            p=s.get('protocolSection',{}); i=p.get('identificationModule',{}); st=p.get('statusModule',{}); de=p.get('descriptionModule',{}); des=p.get('designModule',{}); n=i.get('nctId','')
            desc=' '.join(filter(None,[de.get('briefSummary',''),de.get('detailedDescription','')]))
            out.append({'title':f"{i.get('briefTitle','')} ({st.get('overallStatus','')})",'url':f'https://clinicaltrials.gov/study/{n}','source':'ClinicalTrials.gov','published':st.get('studyFirstPostDateStruct',{}).get('date',''),'description':clean(desc)[:10000],'status':st.get('overallStatus',''),'phase':', '.join(des.get('phases',[]))})
        return out
    except Exception as e: print('ClinicalTrials-Fehler:',e); return []

def article_text(url):
    try:
        r=requests.get(url,headers=H,timeout=35); r.raise_for_status(); s=BeautifulSoup(r.text,'html.parser')
        for x in s(['script','style','nav','footer','header','aside','form']): x.decompose()
        m=s.find('article') or s.find('main') or s.body
        return clean(m.get_text(' ',strip=True) if m else '')[:16000]
    except Exception as e: print('Artikel-Scrape-Fehler:',e); return ''

def ai(title,source,url,body,topic,phase=''):
    key=os.getenv('GEMINI_API_KEY')
    if not key: print('FEHLER: GEMINI_API_KEY fehlt'); return {}
    if not body or len(body)<40: print('Kein ausreichender Volltext:',title[:80]); return {}
    prompt=f'''Du bist wissenschaftlicher Redakteur für einen deutschen Forschungsmonitor zu Usher-Syndrom Typ 1B (USH1B/MYO7A, LUCE-1, AAVB-081) und Typ-2-Diabetes.
Quelle: {source}\nOriginaltitel: {title}\nURL: {url}\nThema: {topic}\nStudienstatus/Phase: {phase}\n\nOriginalinhalt:\n{body[:9000]}\n\nErstelle eine echte deutsche redaktionelle Zusammenfassung. Übersetze nicht nur den Titel. Erkläre verständlich Thema, Studiendesign, Ziel, Teilnehmerzahl falls vorhanden, Status, Ergebnisse, Sicherheit, Wirksamkeit und Einschränkungen soweit im Original vorhanden. Nichts erfinden. Bei laufenden Studien keine Ergebnisse erfinden.
Antworte NUR als JSON mit genau diesen Feldern: title_de, summary_de, detailed_summary_de, why_relevant, country, evidence_key, study_phase.
title_de muss deutsch sein; Fachbegriffe wie AAVB-081, MYO7A und USH1B dürfen unverändert bleiben. summary_de: 2-3 Sätze. detailed_summary_de: ausführliche deutsche Zusammenfassung mit mehreren Sätzen, nicht bloß Wiederholung. why_relevant: konkrete Relevanz. country: Land/Region. evidence_key: bei Unklarheit frueh. study_phase: z.B. Phase 1/2, Phase 3, Beobachtungsstudie, Studienregister, Präklinisch oder Nicht angegeben.'''
    payload={'contents':[{'parts':[{'text':prompt}]}],'generationConfig':{'temperature':0.1,'maxOutputTokens':2200,'responseMimeType':'application/json'}}
    for attempt in range(2):
        try:
            r=requests.post('https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent',headers={'x-goog-api-key':key,'Content-Type':'application/json'},json=payload,timeout=120); r.raise_for_status(); c=r.json()['candidates'][0]['content']['parts'][0]['text'].strip(); c=re.sub(r'^```json\s*|\s*```$','',c,flags=re.I).strip(); return json.loads(c)
        except Exception as e:
            print(f'Gemini-Fehler Versuch {attempt+1}/2:',e)
            if attempt==0: time.sleep(5)
    return {}

def prio(t):
    s=t.lower(); base=sum(p for w,p in [('luce-1',100),('aavb-081',100),('usher 1b',95),('ush1b',95),('myo7a',90),('gene therapy',70),('clinical trial',65),('type 2 diabetes',50),('glp-1',45),('gip',45)] if w in s); return base+sum(15 for term in PRIORITY_TERMS if term and term in s)
def display_date(pub):
    try:
        pd=datetime.fromisoformat(pub.replace('Z','+00:00')) if 'T' in pub else datetime.strptime(pub,'%Y-%m-%d'); return pd.strftime('%d.%m.%Y')
    except Exception:return pub

def main():
    U_GDELT=['"Usher 1B" OR USH1B OR MYO7A','"LUCE-1" OR "AAVB-081"','MYO7A "gene therapy"','"Usher syndrome" gene therapy']; D_GDELT=['"type 2 diabetes" new treatment','"type 2 diabetes" clinical trial','"type 2 diabetes" GLP-1 GIP']; U_PUBMED=['"Usher 1B" OR USH1B OR MYO7A','"LUCE-1" OR "AAVB-081"','MYO7A gene therapy']; D_PUBMED=['"type 2 diabetes" new treatment','"type 2 diabetes" clinical trial','"type 2 diabetes" GLP-1 GIP']
    raw=[]
    for q in U_GDELT:
        for a in gdelt(q): a['assumed_topic']='usher'; raw.append(a)
    for q in D_GDELT:
        for a in gdelt(q): a['assumed_topic']='diabetes'; raw.append(a)
    for q in U_PUBMED:
        for a in pubmed_search(q): a['assumed_topic']='usher'; raw.append(a)
    for q in D_PUBMED:
        for a in pubmed_search(q): a['assumed_topic']='diabetes'; raw.append(a)
    for a in trials('Usher syndrome type 1B MYO7A'): a['assumed_topic']='usher'; raw.append(a)
    for a in trials('type 2 diabetes'): a['assumed_topic']='diabetes'; raw.append(a)
    seen=set(); cand=[]
    for x in raw:
        t=clean(x.get('title','')); u=x.get('url','')
        if t and u and u not in seen and x.get('assumed_topic'): seen.add(u); x['title']=t; cand.append(x)
    cand.sort(key=lambda x:prio(x['title']),reverse=True); items=[]
    for x in cand[:15]:
        t,u=x['title'],x['url']; src=x.get('source') or x.get('domain') or 'Unbekannte Quelle'; topic=x['assumed_topic']
        if src=='ClinicalTrials.gov': body=x.get('description',''); phase=x.get('phase','') or x.get('status','')
        elif src=='PubMed': body=pubmed_abstract(x.get('pmid','')); phase=''
        else: body=article_text(u); phase=''
        a=ai(t,src,u,body,topic,phase); td=clean(a.get('title_de','')) or t; sm=clean(a.get('summary_de','')) or 'Für diese Meldung konnte noch keine deutsche Kurzfassung erzeugt werden.'; ds=clean(a.get('detailed_summary_de','')) or sm; wr=clean(a.get('why_relevant','')) or 'Thematisch relevante Meldung.'; country=clean(a.get('country','')); evidence_key=clean(a.get('evidence_key','')) or 'frueh'; study_phase=clean(a.get('study_phase','')) or phase
        items.append({'id':gid(t,u),'title':t,'title_de':td,'summary_de':sm,'detailed_summary_de':ds,'why_relevant':wr,'url':u,'source':src,'country':country,'published':x.get('published',''),'published_display':display_date(x.get('published','')),'topic':topic,'category':'clinicaltrials' if src=='ClinicalTrials.gov' else ('aavantgarde' if 'aavantgarde' in src.lower() else 'forschung'),'evidence':evidence_key,'evidence_key':evidence_key,'study_phase':study_phase,'priority':prio(t)}); time.sleep(4)
    result={'schema_version':'6.0','generated_at':NOW.isoformat(),'generated_at_display':NOW.strftime('%d.%m.%Y %H:%M UTC'),'sources_checked':len(U_GDELT)+len(D_GDELT)+len(U_PUBMED)+len(D_PUBMED)+2,'items':items}
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8'); (ARCHIVE/NOW.strftime('%Y-%m-%d.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8'); print(f'Fertig: {len(items)} Meldungen')
if __name__=='__main__': main()

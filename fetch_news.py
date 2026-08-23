import os, json, re, hashlib, time
from datetime import datetime, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'data'; ARCHIVE=DATA/'archive'; DATA.mkdir(exist_ok=True); ARCHIVE.mkdir(exist_ok=True)
OUT=DATA/'news.json'; NOW=datetime.now(timezone.utc)
H={'User-Agent':'Forschungsmonitor/8.0','Accept-Language':'de-DE,de;q=0.9,en;q=0.8'}
S=requests.Session(); S.headers.update(H)
CONFIG=json.loads((ROOT/'config.json').read_text(encoding='utf-8')) if (ROOT/'config.json').exists() else {}
PRIORITY=[x.lower() for x in CONFIG.get('usher',{}).get('priority',[])+CONFIG.get('diabetes',{}).get('priority',[])]

def clean(x): return re.sub(r'\s+',' ',BeautifulSoup(str(x or ''),'html.parser').get_text(' ',strip=True)).strip()
def gid(t,u): return hashlib.sha256((t+'|'+u).encode()).hexdigest()[:20]
def get(url,params=None,timeout=30):
    try:
        r=S.get(url,params=params,timeout=timeout); r.raise_for_status(); return r
    except Exception as e: print('HTTP-Fehler:',url,e); return None

def google_news(q,topic):
    r=get('https://news.google.com/rss/search',{'q':q,'hl':'de','gl':'DE','ceid':'DE:de'})
    if not r:return []
    try:
        soup=BeautifulSoup(r.text,'xml'); out=[]
        for z in soup.find_all('item')[:15]:
            title=clean(z.find('title').get_text() if z.find('title') else '')
            url=clean(z.find('link').get_text() if z.find('link') else '')
            if title and url: out.append({'title':title,'url':url,'source':clean(z.find('source').get_text() if z.find('source') else 'Google News'),'published':clean(z.find('pubDate').get_text() if z.find('pubDate') else ''),'body':clean(z.find('description').get_text() if z.find('description') else ''),'topic':topic})
        return out
    except Exception as e: print('RSS-Fehler:',e); return []

def europe_pmc(q,topic):
    r=get('https://www.ebi.ac.uk/europepmc/webservices/rest/search',{'query':q,'format':'json','pageSize':15,'sort':'FIRST_PDATE_D desc','resultType':'core'},40)
    if not r:return []
    try:
        out=[]
        for x in r.json().get('resultList',{}).get('result',[]):
            pmid=str(x.get('pmid') or x.get('id') or ''); title=clean(x.get('title',''))
            if title and pmid: out.append({'title':title,'url':f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/','source':'PubMed / Europe PMC','published':str(x.get('firstPublicationDate') or x.get('pubYear') or ''),'body':clean(x.get('abstractText','')),'topic':topic})
        return out
    except Exception as e: print('Europe-PMC-Fehler:',e); return []

def trials(q,topic):
    r=get('https://clinicaltrials.gov/api/v2/studies',{'query.term':q,'pageSize':20},45)
    if not r:return []
    try:
        out=[]
        for s in r.json().get('studies',[]):
            p=s.get('protocolSection',{}); i=p.get('identificationModule',{}); st=p.get('statusModule',{}); d=p.get('descriptionModule',{}); des=p.get('designModule',{})
            n=i.get('nctId',''); title=clean(i.get('briefTitle',''))
            if not n or not title: continue
            status=st.get('overallStatus','Status unbekannt'); phase=', '.join(des.get('phases',[]))
            body=clean(' '.join(filter(None,[d.get('briefSummary',''),d.get('detailedDescription','')])))
            out.append({'title':title,'url':f'https://clinicaltrials.gov/study/{n}','source':'ClinicalTrials.gov','published':st.get('studyFirstPostDateStruct',{}).get('date',''),'body':body,'topic':topic,'phase':phase,'status':status})
        return out
    except Exception as e: print('ClinicalTrials-Fehler:',e); return []

def article(url):
    r=get(url,timeout=25)
    if not r:return ''
    try:
        s=BeautifulSoup(r.text,'html.parser')
        for x in s(['script','style','nav','footer','header','aside','form']): x.decompose()
        m=s.find('article') or s.find('main') or s.body
        return clean(m.get_text(' ',strip=True) if m else '')[:12000]
    except:return ''

def free_translate(text):
    text=clean(text)
    if not text:return ''
    chunks=[text[i:i+1500] for i in range(0,len(text),1500)]; result=[]
    for chunk in chunks:
        try:
            r=S.get('https://translate.googleapis.com/translate_a/single',params={'client':'gtx','sl':'auto','tl':'de','dt':'t','q':chunk},timeout=30)
            r.raise_for_status(); data=r.json(); result.append(''.join(p[0] for p in data[0] if p and p[0]))
        except Exception as e:
            print('Kostenlose Übersetzung fehlgeschlagen:',e); result.append(chunk)
        time.sleep(.25)
    return clean(' '.join(result))

def gemini(title,source,url,body,topic,phase):
    key=os.getenv('GEMINI_API_KEY')
    if not key:return {}
    prompt=f'''Du bist wissenschaftlicher Redakteur für einen deutschen Forschungsmonitor. Schreibe vollständig auf Deutsch.
Thema: {topic}\nQuelle: {source}\nOriginaltitel: {title}\nStudienstatus: {phase}\nOriginalinhalt:\n{body[:10000]}
Erstelle eine echte redaktionelle Zusammenfassung, nicht nur eine Übersetzung des Titels. Erkläre Thema, Ziel, Design, Teilnehmer, Status, Ergebnisse, Sicherheit, Wirksamkeit und Einschränkungen, aber erfinde nichts. Bei laufenden Studien keine Ergebnisse erfinden.
Gib ausschließlich gültiges JSON zurück mit: title_de, summary_de, detailed_summary_de, why_relevant, country, evidence_key, study_phase. title_de muss Deutsch sein. detailed_summary_de soll mindestens 5 Sätze enthalten, sofern der Inhalt das zulässt.'''
    payload={'contents':[{'parts':[{'text':prompt}]}],'generationConfig':{'temperature':0.1,'maxOutputTokens':2600,'responseMimeType':'application/json'}}
    for endpoint in ['https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent','https://generativelanguage.googleapis.com/v1/models/gemini-2.5-flash:generateContent']:
        try:
            r=S.post(endpoint,headers={'x-goog-api-key':key,'Content-Type':'application/json'},json=payload,timeout=120)
            if not r.ok: print('Gemini HTTP',r.status_code,r.text[:500]); continue
            c=r.json()['candidates'][0]['content']['parts'][0]['text'].strip(); c=re.sub(r'^```json\s*|\s*```$','',c,flags=re.I).strip(); return json.loads(c)
        except Exception as e: print('Gemini-Fehler:',e)
    return {}

def priority(t):
    s=t.lower(); base=sum(p for w,p in [('luce-1',140),('aavb-081',140),('aavantgarde',135),('usher 1b',120),('ush1b',120),('myo7a',110),('gene therapy',80),('gene editing',75),('clinical trial',65),('type 2 diabetes',50),('glp-1',45),('gip',45),('retatrutide',45)] if w in s)
    return base+sum(15 for x in PRIORITY if x and x in s)

def date_display(v):
    try:return datetime.fromisoformat(v.replace('Z','+00:00')).strftime('%d.%m.%Y') if 'T' in v else datetime.strptime(v[:10],'%Y-%m-%d').strftime('%d.%m.%Y')
    except:return v or ''

def make_item(x):
    title=clean(x.get('title','')); url=x.get('url',''); source=x.get('source','Quelle'); topic=x.get('topic','forschung'); body=clean(x.get('body','')); phase=x.get('phase','') or x.get('status','')
    if source not in ['ClinicalTrials.gov','PubMed / Europe PMC'] and len(body)<500:
        b=article(url)
        if len(b)>len(body):body=b
    a=gemini(title,source,url,body,topic,phase)
    title_de=clean(a.get('title_de','')) or free_translate(title)
    summary=clean(a.get('summary_de',''))
    detailed=clean(a.get('detailed_summary_de',''))
    if not summary:
        tr=free_translate(body[:3000]) if body else free_translate(title)
        summary=tr[:900] if tr else 'Zu dieser Meldung liegt derzeit keine deutsche Zusammenfassung vor.'
    if not detailed:
        tr=free_translate(body[:9000]) if body else summary
        detailed=(f'Diese Meldung betrifft eine registrierte klinische Studie. Status: {x.get("status") or "nicht angegeben"}. Studienphase: {phase or "nicht angegeben"}.\n\n{tr}' if source=='ClinicalTrials.gov' else (tr or summary))
    relevant=clean(a.get('why_relevant','')) or ('Die Meldung ist für den Forschungsbereich Usher-Syndrom Typ 1B / MYO7A relevant.' if topic=='usher' else 'Die Meldung ist für den Forschungsbereich Typ-2-Diabetes relevant.')
    evidence=clean(a.get('evidence_key','')) or 'frueh'; study=clean(a.get('study_phase','')) or phase
    return {'id':gid(title,url),'title':title,'title_de':title_de,'summary_de':summary,'detailed_summary_de':detailed,'why_relevant':relevant,'url':url,'source':source,'country':clean(a.get('country','')),'published':x.get('published',''),'published_display':date_display(str(x.get('published',''))),'topic':topic,'category':'clinicaltrials' if source=='ClinicalTrials.gov' else 'forschung','evidence':evidence,'evidence_key':evidence,'study_phase':study,'priority':priority(title),'translation_ok':True}

def main():
    raw=[]
    for q in ['"Usher 1B" OR USH1B OR MYO7A','"LUCE-1" OR "AAVB-081"','AAVantgarde gene therapy','Usher syndrome gene therapy']:raw+=google_news(q,'usher')
    for q in ['"type 2 diabetes" new treatment','"type 2 diabetes" clinical trial','"type 2 diabetes" GLP-1 GIP','retatrutide diabetes']:raw+=google_news(q,'diabetes')
    raw+=europe_pmc('(USH1B OR "Usher 1B" OR MYO7A) AND (gene therapy OR gene editing OR AAV)','usher')
    raw+=europe_pmc('("type 2 diabetes" OR T2D) AND (GLP-1 OR GIP OR retatrutide OR treatment)','diabetes')
    raw+=trials('Usher syndrome type 1B MYO7A','usher'); raw+=trials('type 2 diabetes GLP-1 GIP','diabetes')
    seen=set(); candidates=[]
    for x in raw:
        if x.get('url') and x.get('title') and x['url'] not in seen:seen.add(x['url']);candidates.append(x)
    candidates.sort(key=lambda x:priority(x['title']),reverse=True)
    items=[]
    for x in candidates[:18]:
        try:items.append(make_item(x))
        except Exception as e:print('Meldungsfehler:',x.get('title'),e)
        time.sleep(.7)
    result={'schema_version':'8.0','generated_at':NOW.isoformat(),'generated_at_display':NOW.strftime('%d.%m.%Y %H:%M UTC'),'sources_checked':{'google_news_rss':True,'europe_pmc':True,'clinicaltrials_gov':True,'gdelt':False,'pubmed_eutils':False},'items':items}
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');(ARCHIVE/NOW.strftime('%Y-%m-%d-%H%M.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Fertig: {len(items)} Meldungen')
if __name__=='__main__':main()

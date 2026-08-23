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
# Ein "echter" Browser-User-Agent + deutsche Sprachpräferenz, damit Presseseiten uns nicht als Bot blocken.
H={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0 Safari/537.36 Forschungsmonitor/2.0',
   'Accept-Language':'de-DE,de;q=0.9,en;q=0.8'}

CONFIG=json.loads((ROOT/'config.json').read_text(encoding='utf-8'))
EVIDENCE_DEF=CONFIG.get('evidence',{})
PRIORITY_TERMS=[t.lower() for t in (CONFIG.get('usher',{}).get('priority',[])+CONFIG.get('diabetes',{}).get('priority',[]))]

def clean(x): return re.sub(r'\s+',' ',BeautifulSoup(str(x or ''),'html.parser').get_text(' ',strip=True)).strip()
def gid(t,u): return hashlib.sha256((t+'|'+u).encode()).hexdigest()[:20]

# ---------------------------------------------------------------- Quellen ---

def gdelt(q):
    try:
        r=requests.get('https://api.gdeltproject.org/api/v2/doc/doc',
            params={'query':q,'mode':'artlist','format':'json','maxrecords':25,'timespan':'14days','sort':'datedesc'},
            headers=H,timeout=30)
        r.raise_for_status()
        return r.json().get('articles',[])
    except Exception as e:
        print('GDELT-Fehler:',q,'->',e); return []

def pubmed_search(q):
    try:
        b='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
        ids=requests.get(b+'esearch.fcgi',params={'db':'pubmed','term':q,'retmode':'json','retmax':15,'sort':'pub date'},
                          headers=H,timeout=30).json()['esearchresult']['idlist']
        if not ids: return []
        d=requests.get(b+'esummary.fcgi',params={'db':'pubmed','id':','.join(ids),'retmode':'json'},
                        headers=H,timeout=30).json()['result']
        return [{'title':d[i].get('title',''),'url':f'https://pubmed.ncbi.nlm.nih.gov/{i}/',
                  'source':'PubMed','published':d[i].get('pubdate',''),'pmid':i} for i in ids]
    except Exception as e:
        print('PubMed-Fehler:',q,'->',e); return []

def pubmed_abstract(pmid):
    """Holt den echten Abstract-Text über die PubMed-API - zuverlässiger als die Webseite zu scrapen."""
    if not pmid: return ''
    try:
        r=requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi',
            params={'db':'pubmed','id':pmid,'rettype':'abstract','retmode':'text'},headers=H,timeout=30)
        r.raise_for_status()
        return clean(r.text)[:8000]
    except Exception as e:
        print('PubMed-Abstract-Fehler:',pmid,'->',e); return ''

def trials(q):
    """WICHTIG: nutzt ausschließlich die offizielle ClinicalTrials.gov-API inkl. Beschreibungstext.
    Die Studien-Webseite (clinicaltrials.gov/study/...) ist eine JavaScript-App (Angular) und liefert
    beim einfachen Scrapen praktisch keinen echten Text - das war die Hauptursache dafür, dass diese
    Meldungen bisher nie eine deutsche Übersetzung bekommen haben."""
    try:
        r=requests.get('https://clinicaltrials.gov/api/v2/studies',params={'query.term':q,'pageSize':20},
                        headers=H,timeout=30)
        r.raise_for_status()
        out=[]
        for s in r.json().get('studies',[]):
            p=s.get('protocolSection',{})
            i=p.get('identificationModule',{}); st=p.get('statusModule',{}); de=p.get('descriptionModule',{})
            n=i.get('nctId','')
            desc=' '.join(filter(None,[de.get('briefSummary',''),de.get('detailedDescription','')]))
            out.append({
                'title':f"{i.get('briefTitle','')} ({st.get('overallStatus','')})",
                'url':f'https://clinicaltrials.gov/study/{n}',
                'source':'ClinicalTrials.gov',
                'published':st.get('studyFirstPostDateStruct',{}).get('date',''),
                'description':clean(desc)[:8000],
                'status':st.get('overallStatus','')
            })
        return out
    except Exception as e:
        print('ClinicalTrials-Fehler:',q,'->',e); return []

def article_text(url):
    """Fallback-Scraper NUR für normale Presse-/Nachrichtenseiten (AAVantgarde, Tübingen, Fachmedien).
    ClinicalTrials.gov und PubMed haben eigene, zuverlässigere Quellen (siehe oben) und laufen NICHT
    mehr über diese Funktion."""
    try:
        r=requests.get(url,headers=H,timeout=35)
        r.raise_for_status()
        s=BeautifulSoup(r.text,'html.parser')
        for x in s(['script','style','nav','footer','header','aside','form']): x.decompose()
        m=s.find('article') or s.find('main') or s.body
        return clean(m.get_text(' ',strip=True) if m else '')[:16000]
    except Exception as e:
        print('Artikel-Scrape-Fehler:',url,'->',e); return ''

# ------------------------------------------------------------ KI-Redaktion ---

def ai(title,source,url,body,topic_):
    token=os.getenv('GITHUB_TOKEN')
    if not token:
        print('Kein GITHUB_TOKEN vorhanden - keine KI-Übersetzung möglich für:',title[:70]); return {}

    ev_desc='\n'.join(f'- {k}: {v}' for k,v in EVIDENCE_DEF.items())
    has_body=bool(body and len(body)>40)
    context=body[:6000] if has_body else '(kein Volltext verfügbar - arbeite ausschließlich mit Titel, Quelle und URL, formuliere entsprechend vorsichtig/allgemein)'

    prompt=f'''Du bist wissenschaftlicher Redakteur für einen deutschen Forschungsmonitor zu Usher-Syndrom Typ 1B (MYO7A, LUCE-1, AAVB-081) und Typ-2-Diabetes.
Thema: {topic_}
Quelle: {source}
Originaltitel: {title}
URL: {url}

Verfügbarer Inhalt:
{context}

Evidenzstufen (wähle für evidence_key GENAU einen dieser Schlüssel):
{ev_desc}

Erstelle ausschließlich auf Basis der verfügbaren Informationen (nichts erfinden; bei fehlendem Volltext das ausdrücklich in detailed_summary_de erwähnen):
- title_de: präzise, verständliche deutsche Überschrift
- summary_de: kurze deutsche Zusammenfassung (2-3 Sätze)
- detailed_summary_de: ausführliche deutsche Zusammenfassung (Studiendesign, Status, Ergebnisse, Sicherheit/Wirksamkeit, Einschränkungen - soweit bekannt)
- why_relevant: konkrete Relevanz für USH1B/MYO7A/LUCE-1/AAVB-081 bzw. Typ-2-Diabetes
- category: einer von aavantgarde, clinicaltrials, tuebingen, china, klinische_studien, medikamente, forschung, sonstige
- country: Herkunftsland/-region der Meldung (z. B. Deutschland, Italien, USA, China)
- evidence_key: einer der oben genannten Schlüssel
- study_phase: kurze Statusangabe (z. B. "Phase 1/2", "Präklinisch", "Zugelassen", "Studienregister")

Antworte NUR als minifiziertes JSON-Objekt mit genau diesen acht Feldern, keine Markdown-Codeblöcke, keine weiteren Erklärungen.'''

    for attempt in range(2):
        try:
            r=requests.post('https://models.github.ai/inference/chat/completions',
                headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'},
                json={'model':'openai/gpt-4.1-mini',
                      'messages':[{'role':'system','content':'Du schreibst sorgfältig, sachlich und verständlich auf Deutsch. Du erfindest keine Fakten.'},
                                  {'role':'user','content':prompt}],
                      'temperature':0.1,'max_tokens':1400},
                timeout=120)
            r.raise_for_status()
            c=r.json()['choices'][0]['message']['content'].strip()
            c=re.sub(r'^```json\s*|\s*```$','',c,flags=re.I).strip()
            fb,lb=c.find('{'),c.rfind('}')
            if fb!=-1 and lb!=-1: c=c[fb:lb+1]
            return json.loads(c)
        except Exception as e:
            print(f'KI-Fehler (Versuch {attempt+1}/2) für "{title[:60]}":',e)
            time.sleep(3)
    return {}

# ------------------------------------------------------------- Einstufung ---

def prio(t):
    s=t.lower()
    base=sum(p for w,p in [('luce-1',100),('aavb-081',100),('usher 1b',95),('ush1b',95),('myo7a',90),
        ('gene therapy',70),('clinical trial',65),('type 2 diabetes',50),('glp-1',45),('gip',45)] if w in s)
    # config.json-Prioritätsliste fließt jetzt tatsächlich in die Sortierung ein
    base+=sum(15 for term in PRIORITY_TERMS if term and term in s)
    return base

# ------------------------------------------------------------------ Main ---

def main():
    U_GDELT=['"Usher 1B" OR USH1B OR MYO7A','"LUCE-1" OR "AAVB-081"','MYO7A "gene therapy"',
             '"Usher syndrome" gene therapy','MYO7A sourcelang:chinese','"Usher syndrome" sourcelang:chinese']
    D_GDELT=['"type 2 diabetes" new treatment','"type 2 diabetes" clinical trial','"type 2 diabetes" GLP-1 GIP',
             '"type 2 diabetes" sourcelang:chinese','diabetes gene therapy sourcelang:chinese']
    U_PUBMED=['"Usher 1B" OR USH1B OR MYO7A','"LUCE-1" OR "AAVB-081"','MYO7A gene therapy',
              '(MYO7A OR "Usher syndrome") AND China[Affiliation]']
    D_PUBMED=['"type 2 diabetes" new treatment','"type 2 diabetes" clinical trial',
              '"type 2 diabetes" AND China[Affiliation]']

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
        if t and u and u not in seen and x.get('assumed_topic'):
            seen.add(u); x['title']=t; cand.append(x)

    cand.sort(key=lambda x:prio(x['title']),reverse=True)
    items=[]
    for x in cand[:30]:
        t,u=x['title'],x['url']
        src=x.get('source') or x.get('domain') or 'Unbekannte Quelle'
        topic_=x['assumed_topic']

        # Textquelle je nach Herkunft - NICHT mehr die JS-Seite von ClinicalTrials.gov scrapen
        if src=='ClinicalTrials.gov':
            body=x.get('description','')
        elif src=='PubMed':
            body=pubmed_abstract(x.get('pmid',''))
        else:
            body=article_text(u)

        a=ai(t,src,u,body,topic_)

        td=clean(a.get('title_de','')) or t
        sm=clean(a.get('summary_de','')) or f'Automatische Übersetzung derzeit nicht verfügbar. Originaltitel: {t}'
        ds=clean(a.get('detailed_summary_de','')) or sm
        wr=clean(a.get('why_relevant','')) or 'Thematisch relevante Meldung.'
        category=clean(a.get('category','')) or ('clinicaltrials' if src=='ClinicalTrials.gov' else '')
        country=clean(a.get('country',''))
        evidence_key=clean(a.get('evidence_key','')) or 'frueh'
        evidence=EVIDENCE_DEF.get(evidence_key,'')
        study_phase=clean(a.get('study_phase','')) or x.get('status','')

        pub=x.get('published',''); pub_display=pub
        try:
            pd=datetime.fromisoformat(pub.replace('Z','+00:00')) if 'T' in pub else datetime.strptime(pub,'%Y-%m-%d')
            pub_display=pd.strftime('%d.%m.%Y')
        except Exception:
            pass

        items.append({
            'id':gid(t,u),'title':t,'title_de':td,'summary_de':sm,'detailed_summary_de':ds,'why_relevant':wr,
            'url':u,'source':src,'country':country,'published':pub,'published_display':pub_display,
            'topic':topic_,'category':category,'evidence':evidence,'evidence_key':evidence_key,
            'study_phase':study_phase,'priority':prio(t)
        })
        time.sleep(1)

    result={'schema_version':'5.0','generated_at':NOW.isoformat(),
            'generated_at_display':NOW.strftime('%d.%m.%Y %H:%M UTC'),
            'sources_checked':len(U_GDELT)+len(D_GDELT)+len(U_PUBMED)+len(D_PUBMED)+2,
            'items':items}
    OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (ARCHIVE/NOW.strftime('%Y-%m-%d.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')

    translated=sum(1 for i in items if i['title_de']!=i['title'])
    print(f'Fertig: {len(items)} Meldungen, {translated} davon mit echter deutscher KI-Übersetzung')

if __name__=='__main__':
    main()

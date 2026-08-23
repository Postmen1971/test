import os, re, json, hashlib
from datetime import datetime, timezone
from pathlib import Path
import requests
import feedparser
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parent
DATA=ROOT/"data"; ARCHIVE=DATA/"archive"
DATA.mkdir(exist_ok=True); ARCHIVE.mkdir(exist_ok=True)
OUT=DATA/"news.json"
HEADERS={"User-Agent":"Usher-Diabetes-Research-Monitor/2.0 (+GitHub Actions)"}
NOW=datetime.now(timezone.utc)

USHER_QUERIES=[
    '"Usher 1B" OR USH1B OR MYO7A',
    '"LUCE-1" OR "AAVB-081"',
    '"MYO7A" AND (gene therapy OR AAV OR "gene editing")',
    '"Usher syndrome" AND (gene therapy OR AAV OR CRISPR)',
    '"Usher 1B" China OR 中国 OR MYO7A',
]
DIABETES_QUERIES=[
    '"type 2 diabetes" AND ("gene therapy" OR "gene editing" OR AAV OR CRISPR)',
    '"type 2 diabetes" AND (new drug OR novel therapy OR trial)',
    '"type 2 diabetes" AND (GLP-1 OR GIP OR retatrutide OR insulin)',
    'diabetes CGM reimbursement Germany AOK',
    'Diabetes Typ 2 AOK Baden-Württemberg CGM',
]

WATCH_URLS=[
 ("tuebingen","https://www.medizin.uni-tuebingen.de/de/das-klinikum/einrichtungen/kliniken/augenklinik/ambulanzen-sprechstunden/erbliche-netzhautdegenerationen"),
 ("aok","https://www.aok.de/pk/bw/"),
 ("aok","https://www.aok.de/pk/leistungen/hilfsmittel/kontinuierliche-glukosemessung-rtcgm/"),
 ("aavantgarde","https://www.aavantgarde.com/en/news/"),
]

def get(url,timeout=30):
    try:
        r=requests.get(url,headers=HEADERS,timeout=timeout); r.raise_for_status(); return r
    except Exception: return None

def clean(s):
    return re.sub(r"\s+"," ",BeautifulSoup(str(s or ""), "html.parser").get_text(" ",strip=True)).strip()

def norm_title(s):
    return re.sub(r"\s+"," ",re.sub(r"[^a-z0-9äöüß ]"," ",s.lower())).strip()

def make_id(title,url):
    return hashlib.sha256((norm_title(title)+"|"+url.split("?")[0]).encode()).hexdigest()[:20]

def country_from(s):
    s=s.lower()
    if any(x in s for x in ["china","中国","beijing","shanghai","shenzhen"]): return "China"
    if any(x in s for x in ["germany","deutschland",".de","tübingen","tuebingen"]): return "Deutschland"
    if any(x in s for x in ["italy","italia",".it"]): return "Italien"
    if any(x in s for x in ["united states",".us","usa"]): return "USA"
    if any(x in s for x in ["united kingdom","uk",".uk","england"]): return "Großbritannien"
    return ""

def gdelt(query):
    try:
        data=requests.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params={"query":query,"mode":"artlist","format":"json","maxrecords":50,"timespan":"7days","sort":"datedesc"},
            headers=HEADERS,timeout=30).json()
        return [{"title":a.get("title",""),"url":a.get("url",""),"published":a.get("seendate",""),
                 "source":a.get("domain","GDELT"),"country":country_from(a.get("title","")+" "+a.get("domain",""))}
                for a in data.get("articles",[])]
    except Exception: return []

def pubmed(query):
    base="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    p={"db":"pubmed","term":query+" AND 2026[pdat]","retmode":"json","retmax":"25","sort":"pub date","tool":"usher_diabetes_monitor","email":os.getenv("NCBI_EMAIL","")}
    try:
        ids=requests.get(base+"esearch.fcgi",params=p,headers=HEADERS,timeout=30).json()["esearchresult"]["idlist"]
        if not ids: return []
        d=requests.get(base+"esummary.fcgi",params={"db":"pubmed","id":",".join(ids),"retmode":"json"},headers=HEADERS,timeout=30).json()["result"]
        return [{"title":d.get(i,{}).get("title",""),"url":f"https://pubmed.ncbi.nlm.nih.gov/{i}/","published":d.get(i,{}).get("pubdate",""),"source":"PubMed","country":""} for i in ids]
    except Exception: return []

def clinical_trials(query):
    try:
        data=requests.get("https://clinicaltrials.gov/api/v2/studies",
            params={"query.term":query,"pageSize":20,"format":"json"},headers=HEADERS,timeout=30).json()
        out=[]
        for st in data.get("studies",[]):
            p=st.get("protocolSection",{}); i=p.get("identificationModule",{}); s=p.get("statusModule",{})
            nct=i.get("nctId",""); title=i.get("briefTitle","")
            out.append({"title":f"{title} ({s.get('overallStatus','')})","url":f"https://clinicaltrials.gov/study/{nct}",
                        "published":s.get("studyFirstPostDateStruct",{}).get("date",""),"source":"ClinicalTrials.gov","country":""})
        return out
    except Exception: return []

def watch_pages():
    out=[]
    for category,url in WATCH_URLS:
        r=get(url)
        if not r: continue
        soup=BeautifulSoup(r.text,"html.parser"); text=clean(soup.get_text(" ",strip=True))
        if any(t in text.lower() for t in ["usher","myo7a","retinitis","diabetes","cgms","glukose","aok"]):
            out.append({"title":clean(soup.title.string if soup.title else url),"url":url,
                        "published":NOW.date().isoformat(),"source":category.upper(),"country":"Deutschland","category":category})
    return out

def classify(title,source="",url=""):
    s=(title+" "+source+" "+url).lower()
    if any(x in s for x in ["usher","ush1b","myo7a","luce-1","aavb-081"]): return "usher"
    if any(x in s for x in ["diabetes","aok","cgm","glucose","glukose","retatrutide","glp-1","gip"]): return "diabetes"
    return "other"

def evidence(title,source):
    s=(title+" "+source).lower()
    if "clinicaltrials.gov" in s: return ("Studienregister","registry")
    if "phase 3" in s: return ("Klinische Forschung","clinical")
    if "phase 1" in s or "phase 2" in s: return ("Frühe klinische Forschung","frueh")
    if "pubmed" in s: return ("Wissenschaftliche Publikation","peer_review")
    return ("Forschung – Einordnung prüfen","research")

def relevance(title):
    s=title.lower(); score=0
    for term,pts in [("luce-1",100),("aavb-081",100),("usher 1b",95),("ush1b",95),("myo7a",90),("clinical trial",75),("phase 3",80),("gene therapy",65),("aok",85),("cgm",70),("retatrutide",65),("type 2 diabetes",55)]:
        if term in s: score+=pts
    return score

def translate_google(text):
    text=clean(text)[:6000]
    if not text: return ""
    result=[]
    for i in range(0,len(text),1800):
        chunk=text[i:i+1800]
        try:
            r=requests.get("https://translate.googleapis.com/translate_a/single",
                params={"client":"gtx","sl":"auto","tl":"de","dt":"t","q":chunk},headers=HEADERS,timeout=30)
            data=r.json()
            result.append("".join(x[0] for x in data[0] if x and x[0]))
        except Exception: pass
    return clean(" ".join(result))

def article_text(url):
    r=get(url)
    if not r: return ""
    soup=BeautifulSoup(r.text,"html.parser")
    for tag in soup(["script","style","nav","footer","header","aside","form"]): tag.decompose()
    main=soup.find("article") or soup.find("main") or soup.body
    return clean(main.get_text(" ",strip=True) if main else "")

def openai_summary(title,text):
    key=os.getenv("OPENAI_API_KEY")
    if not key or not text: return ""
    prompt=f"""Erstelle für einen deutschen Forschungsmonitor eine ausführliche, gut verständliche deutsche Zusammenfassung.
Titel: {title}
Originaltext:
{text[:12000]}
Nenne nur Informationen, die im Originaltext stehen. Berücksichtige Ergebnisse, Studiendesign, Patientenzahl, Sicherheit, Wirksamkeit, Einschränkungen und Bedeutung, sofern vorhanden. Keine erfundenen Fakten. 5-10 Absätze plus einen kurzen Abschnitt 'Einordnung:'."""
    try:
        r=requests.post("https://api.openai.com/v1/responses",
            headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},
            json={"model":os.getenv("OPENAI_MODEL","gpt-4o-mini"),"input":prompt},timeout=90)
        r.raise_for_status()
        return clean(r.json().get("output_text",""))
    except Exception: return ""

def detailed_summary(title,url):
    text=article_text(url)
    if not text: return "Der Originaltext konnte automatisch nicht geladen werden. Bitte die Originalquelle öffnen."
    ai=openai_summary(title,text)
    if ai: return ai
    de=translate_google(text)
    if de:
        return "Ausführliche deutsche Inhaltsdarstellung:\n\n"+de+"\n\nEinordnung: Automatisch aus dem abrufbaren Originaltext ins Deutsche übertragen. Für die maßgebliche Originalfassung bitte die Originalquelle öffnen."
    return "Der Originaltext konnte automatisch nicht übersetzt werden. Bitte die Originalquelle öffnen."

def dedupe(items):
    seen=set(); out=[]
    for x in sorted(items,key=lambda z:(z.get("priority",0),z.get("published","")),reverse=True):
        k=norm_title(x.get("title",""))
        if k in seen: continue
        seen.add(k); out.append(x)
    return out

def main():
    raw=[]
    for q in USHER_QUERIES+DIABETES_QUERIES: raw+=gdelt(q)
    for q in USHER_QUERIES[:3]+DIABETES_QUERIES[:4]: raw+=pubmed(q)
    raw+=clinical_trials("Usher syndrome type 1B MYO7A")
    raw+=clinical_trials("type 2 diabetes")
    raw+=watch_pages()

    items=[]
    for r in raw:
        title=clean(r.get("title","")); url=r.get("url","")
        if not title or not url: continue
        topic=classify(title,r.get("source",""),url)
        if topic=="other": continue
        ev,evk=evidence(title,r.get("source",""))
        print("Verarbeite:",title[:100])
        title_de=translate_google(title) or title
        details=detailed_summary(title,url)
        items.append({
            "id":make_id(title,url),"title":title,"title_de":title_de,
            "summary_de":details[:500]+("…" if len(details)>500 else ""),
            "detailed_summary_de":details,"why_relevant":"",
            "url":url,"source":r.get("source",""),
            "country":r.get("country","") or country_from(title+" "+url),
            "published":r.get("published",""),"published_display":r.get("published",""),
            "topic":topic,"category":r.get("category",""),
            "evidence":ev,"evidence_key":evk,"priority":relevance(title)
        })

    items=dedupe(items)[:100]
    payload={"schema_version":"3.0","generated_at":NOW.isoformat(),
             "generated_at_display":NOW.strftime("%d.%m.%Y %H:%M UTC"),
             "sources_checked":len(USHER_QUERIES)+len(DIABETES_QUERIES)+3+2+len(WATCH_URLS),
             "items":items}
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    (ARCHIVE/(NOW.strftime("%Y-%m-%d")+".json")).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Gespeichert: {len(items)} Meldungen")

if __name__=="__main__": main()

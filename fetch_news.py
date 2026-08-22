import os, re, json, hashlib, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import requests
import feedparser
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parent
DATA=ROOT/"data"
ARCHIVE=DATA/"archive"
DATA.mkdir(exist_ok=True); ARCHIVE.mkdir(exist_ok=True)
OUT=DATA/"news.json"
HEADERS={"User-Agent":"Usher-Diabetes-Research-Monitor/1.0 (+GitHub Actions)"}
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
    'Diabetes Göppingen Plochingen Esslingen Versorgung',
]

WATCH_URLS=[
 ("tuebingen","https://www.medizin.uni-tuebingen.de/de/das-klinikum/einrichtungen/kliniken/augenklinik/ambulanzen-sprechstunden/erbliche-netzhautdegenerationen"),
 ("aok","https://www.aok.de/pk/bw/"),
 ("aok","https://www.aok.de/pk/leistungen/hilfsmittel/kontinuierliche-glukosemessung-rtcgm/"),
 ("aavantgarde","https://www.aavantgarde.com/en/news/"),
]

def get(url,timeout=25):
    try:
        r=requests.get(url,headers=HEADERS,timeout=timeout)
        r.raise_for_status(); return r
    except Exception as e:
        return None

def clean(s):
    return re.sub(r"\s+"," ",BeautifulSoup(str(s or ""),"html.parser").get_text(" ",strip=True)).strip()

def norm_title(s):
    s=re.sub(r"[^a-z0-9äöüß ]"," ",s.lower())
    return re.sub(r"\s+"," ",s).strip()

def make_id(title,url):
    return hashlib.sha256((norm_title(title)+"|"+url.split("?")[0]).encode()).hexdigest()[:20]

def gdelt(query):
    url="https://api.gdeltproject.org/api/v2/doc/doc"
    params={"query":query,"mode":"artlist","format":"json","maxrecords":50,"timespan":"1day","sort":"datedesc"}
    r=get(url)
    if not r:return []
    try:
        data=requests.get(url,params=params,headers=HEADERS,timeout=30).json()
        out=[]
        for a in data.get("articles",[]):
            out.append({"title":a.get("title",""),"url":a.get("url",""),"published":a.get("seendate",""),"source":a.get("domain","GDELT"),"country":country_from(a.get("title","")+" "+a.get("domain","")),"lang":a.get("language","")})
        return out
    except Exception:return []

def pubmed(query):
    base="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    p={"db":"pubmed","term":query+" AND 2026[pdat]","retmode":"json","retmax":"25","sort":"pub date","tool":"usher_diabetes_monitor","email":os.getenv("NCBI_EMAIL","")}
    if os.getenv("NCBI_API_KEY"):p["api_key"]=os.environ["NCBI_API_KEY"]
    try:
        ids=requests.get(base+"esearch.fcgi",params=p,headers=HEADERS,timeout=30).json()["esearchresult"]["idlist"]
        if not ids:return []
        s={"db":"pubmed","id":",".join(ids),"retmode":"json","tool":"usher_diabetes_monitor","email":os.getenv("NCBI_EMAIL","")}
        if os.getenv("NCBI_API_KEY"):s["api_key"]=os.environ["NCBI_API_KEY"]
        data=requests.get(base+"esummary.fcgi",params=s,headers=HEADERS,timeout=30).json()["result"]
        out=[]
        for i in ids:
            x=data.get(i,{})
            out.append({"title":x.get("title",""),"url":f"https://pubmed.ncbi.nlm.nih.gov/{i}/","published":x.get("pubdate",""),"source":"PubMed","country":""})
        return out
    except Exception:return []

def clinical_trials(query):
    url="https://clinicaltrials.gov/api/v2/studies"
    try:
        data=requests.get(url,params={"query.term":query,"pageSize":20,"format":"json"},headers=HEADERS,timeout=30).json()
        out=[]
        for st in data.get("studies",[]):
            p=st.get("protocolSection",{}); id_=p.get("identificationModule",{}).get("nctId","")
            title=p.get("identificationModule",{}).get("briefTitle","")
            status=p.get("statusModule",{}).get("overallStatus","")
            out.append({"title":f"{title} ({status})","url":f"https://clinicaltrials.gov/study/{id_}","published":p.get("statusModule",{}).get("studyFirstPostDateStruct",{}).get("date",""),"source":"ClinicalTrials.gov","country":""})
        return out
    except Exception:return []

def watch_pages():
    out=[]
    for category,url in WATCH_URLS:
        r=get(url)
        if not r:continue
        soup=BeautifulSoup(r.text,"html.parser")
        title=clean(soup.title.string if soup.title else url)
        text=clean(soup.get_text(" ",strip=True))
        # Only create a signal when the page has target terms. Hash lets the archive detect changes.
        targets=["usher","myo7a","retinitis","diabetes","cgms","glukose","aok"]
        if any(t in text.lower() for t in targets):
            out.append({"title":title,"url":url,"published":NOW.date().isoformat(),"source":category.upper(),"country":"Deutschland","category":category,"page_hash":hashlib.sha256(text.encode()).hexdigest()})
    return out

def country_from(s):
    s=s.lower()
    if any(x in s for x in ["china","中国","beijing","shanghai","shenzhen"]):return "China"
    if any(x in s for x in ["germany","deutschland",".de","tübingen","tuebingen"]):return "Deutschland"
    if any(x in s for x in ["italy","italia",".it"]):return "Italien"
    if any(x in s for x in ["japan",".jp","tokyo"]):return "Japan"
    if any(x in s for x in ["korea",".kr","seoul"]):return "Südkorea"
    if any(x in s for x in ["united kingdom","uk",".uk","england"]):return "Großbritannien"
    if any(x in s for x in ["france",".fr"]):return "Frankreich"
    if any(x in s for x in ["united states",".us","usa"]):return "USA"
    return ""

def classify(title,source="",url=""):
    s=(title+" "+source+" "+url).lower()
    if any(x in s for x in ["usher","ush1b","myo7a","luce-1","aavb-081"]): return "usher"
    if any(x in s for x in ["diabetes","aok","cgm","glucose","glukose","retatrutide","glp-1","gip"]): return "diabetes"
    return "other"

def evidence(title,source):
    s=(title+" "+source).lower()
    if "clinicaltrials.gov" in s or "phase 3" in s:return ("Interessant","interessant")
    if "phase 1" in s or "phase 2" in s:return ("Früh","frueh")
    if "pubmed" in s:return ("Wissenschaftliche Publikation","interessant")
    if any(x in s for x in ["gene therapy","aav","crispr","gene editing","gentherapie"]):return ("Präklinisch/unklar – prüfen","praeklinisch")
    return ("Unklar","ansatz")

def relevance(title):
    s=title.lower()
    score=0
    for term,pts in [("luce-1",100),("aavb-081",100),("usher 1b",95),("ush1b",95),("myo7a",90),("clinical trial",75),("phase 3",80),("gene therapy",65),("aok",85),("cgm",70),("retatrutide",65),("type 2 diabetes",55)]:
        if term in s:score+=pts
    return score

def simple_summary(title):
    return "Neue Information gefunden. Die Originalquelle sollte für die vollständige Einordnung geöffnet werden."

def dedupe(items):
    seen=set(); out=[]
    for x in sorted(items,key=lambda z:(z.get("priority",0),z.get("published","")),reverse=True):
        key=norm_title(x.get("title",""))
        if key in seen:continue
        seen.add(key);out.append(x)
    return out

def main():
    raw=[]
    for q in USHER_QUERIES+DIABETES_QUERIES:
        raw += gdelt(q)
    for q in USHER_QUERIES[:3]+DIABETES_QUERIES[:4]:
        raw += pubmed(q)
    raw += clinical_trials("Usher syndrome type 1B MYO7A")
    raw += clinical_trials("type 2 diabetes")
    raw += watch_pages()

    items=[]
    for r in raw:
        title=clean(r.get("title",""))
        url=r.get("url","")
        if not title or not url:continue
        topic=classify(title,r.get("source",""),url)
        if topic=="other":continue
        ev,evk=evidence(title,r.get("source",""))
        item={
            "id":make_id(title,url),"title":title,"title_de":title,"summary_de":simple_summary(title),
            "why_relevant":"","url":url,"source":r.get("source",""),"country":r.get("country",""),
            "published":r.get("published",""),"published_display":r.get("published",""),
            "topic":topic,"category":r.get("category",""),"evidence":ev,"evidence_key":evk,
            "priority":relevance(title)
        }
        items.append(item)
    items=dedupe(items)[:100]
    generated=NOW.isoformat()
    payload={"generated_at":generated,"generated_at_display":NOW.astimezone().strftime("%d.%m.%Y %H:%M UTC"),"sources_checked":len(USHER_QUERIES)+len(DIABETES_QUERIES)+3+2+len(WATCH_URLS),"items":items}
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    archive=ARCHIVE/(NOW.strftime("%Y-%m-%d")+".json")
    archive.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"Gespeichert: {len(items)} Meldungen")

if __name__=="__main__":
    main()

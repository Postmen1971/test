import os
import json
import re
import hashlib
import time
import html
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ARCHIVE = DATA / "archive"
DATA.mkdir(exist_ok=True)
ARCHIVE.mkdir(exist_ok=True)
OUT = DATA / "news.json"
NOW = datetime.now(timezone.utc)

H = {
    "User-Agent": "Forschungsmonitor/7.0 (+https://github.com/Postmen1971/test)",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8")) if (ROOT / "config.json").exists() else {}
PRIORITY_TERMS = [
    t.lower()
    for t in (
        CONFIG.get("usher", {}).get("priority", [])
        + CONFIG.get("diabetes", {}).get("priority", [])
    )
]

SESSION = requests.Session()
SESSION.headers.update(H)


def clean(x):
    return re.sub(r"\s+", " ", BeautifulSoup(str(x or ""), "html.parser").get_text(" ", strip=True)).strip()


def gid(title, url):
    return hashlib.sha256((title + "|" + url).encode("utf-8")).hexdigest()[:20]


def get(url, params=None, timeout=30):
    try:
        r = SESSION.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"HTTP-Fehler bei {url}: {e}")
        return None


def google_news_rss(query, topic):
    """Robuste News-Suche ohne GDELT. Google-News-RSS liefert XML und braucht keinen API-Key."""
    url = "https://news.google.com/rss/search"
    r = get(url, {"q": query, "hl": "de", "gl": "DE", "ceid": "DE:de"}, timeout=30)
    if not r:
        return []
    try:
        soup = BeautifulSoup(r.text, "xml")
        out = []
        for item in soup.find_all("item")[:20]:
            title = clean(item.find("title").get_text() if item.find("title") else "")
            link = clean(item.find("link").get_text() if item.find("link") else "")
            pub = clean(item.find("pubDate").get_text() if item.find("pubDate") else "")
            desc = clean(item.find("description").get_text() if item.find("description") else "")
            source = clean(item.find("source").get_text() if item.find("source") else "Google News")
            if title and link:
                out.append({
                    "title": title,
                    "url": link,
                    "source": source,
                    "published": pub,
                    "body": desc,
                    "assumed_topic": topic,
                })
        return out
    except Exception as e:
        print("Google-News-RSS-Fehler:", e)
        return []


def europe_pmc(query, topic):
    """Europa-PMC statt der fehleranfälligen PubMed-esearch/esummary-Kette.
    Die API liefert Treffer und Abstracts in einer Antwort und benötigt keinen Key."""
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    params = {
        "query": query,
        "format": "json",
        "pageSize": 15,
        "sort": "FIRST_PDATE_D desc",
        "resultType": "core",
    }
    r = get(url, params, timeout=40)
    if not r:
        return []
    try:
        data = r.json()
        out = []
        for x in data.get("resultList", {}).get("result", []):
            pmid = str(x.get("pmid") or x.get("id") or "")
            title = clean(x.get("title", ""))
            if not title or not pmid:
                continue
            abstract = clean(x.get("abstractText", ""))
            date = x.get("firstPublicationDate") or x.get("pubYear", "")
            out.append({
                "title": title,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "source": "PubMed / Europe PMC",
                "published": str(date),
                "body": abstract,
                "assumed_topic": topic,
                "pmid": pmid,
            })
        return out
    except Exception as e:
        print("Europe-PMC-Fehler:", e)
        return []


def clinical_trials(query, topic):
    url = "https://clinicaltrials.gov/api/v2/studies"
    params = {"query.term": query, "pageSize": 20, "format": "json"}
    r = get(url, params, timeout=45)
    if not r:
        return []
    try:
        out = []
        for s in r.json().get("studies", []):
            p = s.get("protocolSection", {})
            ident = p.get("identificationModule", {})
            status = p.get("statusModule", {})
            desc = p.get("descriptionModule", {})
            design = p.get("designModule", {})
            nct = ident.get("nctId", "")
            title = clean(ident.get("briefTitle", ""))
            if not title or not nct:
                continue
            text = " ".join(filter(None, [desc.get("briefSummary", ""), desc.get("detailedDescription", "")]))
            phases = ", ".join(design.get("phases", []))
            out.append({
                "title": f"{title} ({status.get('overallStatus', 'Status unbekannt')})",
                "url": f"https://clinicaltrials.gov/study/{nct}",
                "source": "ClinicalTrials.gov",
                "published": status.get("studyFirstPostDateStruct", {}).get("date", ""),
                "body": clean(text),
                "assumed_topic": topic,
                "phase": phases,
                "status": status.get("overallStatus", ""),
            })
        return out
    except Exception as e:
        print("ClinicalTrials-Fehler:", e)
        return []


def article_text(url):
    """Versucht den Originalartikel zu lesen. Ein Fehler darf die Recherche nie stoppen."""
    r = get(url, timeout=25)
    if not r:
        return ""
    try:
        s = BeautifulSoup(r.text, "html.parser")
        for x in s(["script", "style", "nav", "footer", "header", "aside", "form"]):
            x.decompose()
        m = s.find("article") or s.find("main") or s.body
        return clean(m.get_text(" ", strip=True) if m else "")[:16000]
    except Exception as e:
        print("Artikel-Scrape-Fehler:", e)
        return ""


def priority(title):
    s = title.lower()
    base = sum(
        p
        for word, p in [
            ("luce-1", 140),
            ("aavb-081", 140),
            ("aavantgarde", 135),
            ("usher 1b", 120),
            ("ush1b", 120),
            ("myo7a", 110),
            ("gene therapy", 80),
            ("gene editing", 75),
            ("clinical trial", 65),
            ("type 2 diabetes", 50),
            ("glp-1", 45),
            ("gip", 45),
            ("retatrutide", 45),
        ]
        if word in s
    )
    return base + sum(15 for term in PRIORITY_TERMS if term and term in s)


def display_date(value):
    if not value:
        return ""
    try:
        if "T" in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d.%m.%Y")
        return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return value


def gemini(title, source, url, body, topic, phase=""):
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        print("FEHLER: GEMINI_API_KEY fehlt")
        return {}
    if not body or len(body) < 60:
        return {}

    prompt = f"""Du bist wissenschaftlicher Redakteur für einen deutschen Forschungsmonitor.

Schwerpunkt:
- Usher-Syndrom Typ 1B / USH1B / MYO7A
- LUCE-1 / AAVB-081 / AAVantgarde
- Gentherapie, dual AAV, Gen-Editing und Retinitis pigmentosa
- Typ-2-Diabetes, GLP-1/GIP und neue Therapien

Quelle: {source}
Originaltitel: {title}
URL: {url}
Thema: {topic}
Studienstatus/Phase: {phase}

ORIGINALINHALT:
{body[:12000]}

Aufgabe:
Erstelle eine eigenständige, verständliche deutsche redaktionelle Zusammenfassung. NICHT nur den Titel übersetzen.
Erkläre – soweit im Original vorhanden – worum es geht, Studiendesign, Ziel, Teilnehmerzahl, Status, Ergebnisse, Wirksamkeit, Sicherheit und Einschränkungen.
Bei laufenden Studien dürfen KEINE Ergebnisse erfunden werden.
Wenn die Quelle nur eine kurze Meldung enthält, darfst du nur das sicher belegbare wiedergeben.

Antworte ausschließlich als gültiges JSON mit genau diesen Feldern:
"title_de": deutsche Überschrift,
"summary_de": 2-3 verständliche Sätze,
"detailed_summary_de": ausführliche deutsche Zusammenfassung mit mindestens 5 Sätzen, wenn der Originalinhalt das zulässt,
"why_relevant": konkrete Bedeutung für Patienten/Forschung,
"country": Land/Region,
"evidence_key": stark|interessant|frueh|praeklinisch|ansatz|vorsicht,
"study_phase": Phase 1, Phase 1/2, Phase 2, Phase 3, Beobachtungsstudie, Studienregister, Präklinisch oder Nicht angegeben.
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 2600,
            "responseMimeType": "application/json",
        },
    }

    endpoint = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    for attempt in range(2):
        try:
            r = requests.post(
                endpoint,
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json=payload,
                timeout=120,
            )
            r.raise_for_status()
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.I).strip()
            return json.loads(text)
        except Exception as e:
            print(f"Gemini-Fehler Versuch {attempt + 1}/2: {e}")
            if attempt == 0:
                time.sleep(5)
    return {}


def add_item(items, x):
    title = clean(x.get("title", ""))
    url = x.get("url", "")
    if not title or not url:
        return

    source = x.get("source", "Unbekannte Quelle")
    topic = x.get("assumed_topic", "forschung")
    body = clean(x.get("body", ""))
    phase = x.get("phase", "") or x.get("status", "")

    # Bei News-RSS zunächst versuchen, den Originalartikel zu lesen.
    if source != "ClinicalTrials.gov" and source != "PubMed / Europe PMC" and len(body) < 500:
        original = article_text(url)
        if len(original) > len(body):
            body = original

    ai = gemini(title, source, url, body, topic, phase)
    title_de = clean(ai.get("title_de", "")) or title
    summary = clean(ai.get("summary_de", "")) or "Für diese Meldung konnte noch keine deutsche Kurzfassung erzeugt werden."
    detailed = clean(ai.get("detailed_summary_de", "")) or summary
    relevant = clean(ai.get("why_relevant", "")) or "Thematisch relevante Meldung."
    evidence = clean(ai.get("evidence_key", "")) or "frueh"
    study_phase = clean(ai.get("study_phase", "")) or phase

    if source == "ClinicalTrials.gov":
        category = "clinicaltrials"
    elif any(t in source.lower() for t in ["aavantgarde"]):
        category = "aavantgarde"
    else:
        category = "forschung"

    items.append({
        "id": gid(title, url),
        "title": title,
        "title_de": title_de,
        "summary_de": summary,
        "detailed_summary_de": detailed,
        "why_relevant": relevant,
        "url": url,
        "source": source,
        "country": clean(ai.get("country", "")),
        "published": x.get("published", ""),
        "published_display": display_date(str(x.get("published", ""))),
        "topic": topic,
        "category": category,
        "evidence": evidence,
        "evidence_key": evidence,
        "study_phase": study_phase,
        "priority": priority(title),
    })


def main():
    raw = []

    # 1) Aktuelle Nachrichten – kein GDELT mehr.
    usher_news = [
        '"Usher 1B" OR USH1B OR MYO7A',
        '"LUCE-1" OR "AAVB-081"',
        'AAVantgarde gene therapy',
        'Usher syndrome gene therapy',
    ]
    diabetes_news = [
        '"type 2 diabetes" new treatment',
        '"type 2 diabetes" clinical trial',
        '"type 2 diabetes" GLP-1 GIP',
        'retatrutide diabetes',
    ]
    for q in usher_news:
        raw.extend(google_news_rss(q, "usher"))
    for q in diabetes_news:
        raw.extend(google_news_rss(q, "diabetes"))

    # 2) Wissenschaftliche Veröffentlichungen – Europe PMC statt PubMed-eutils-Kette.
    raw.extend(europe_pmc('(USH1B OR "Usher 1B" OR MYO7A) AND (gene therapy OR gene editing OR AAV)', "usher"))
    raw.extend(europe_pmc('("type 2 diabetes" OR T2D) AND (GLP-1 OR GIP OR retatrutide OR treatment)', "diabetes"))

    # 3) Studienregister.
    raw.extend(clinical_trials('Usher syndrome type 1B MYO7A', "usher"))
    raw.extend(clinical_trials('type 2 diabetes GLP-1 GIP', "diabetes"))

    # Doppelte URLs entfernen.
    seen = set()
    candidates = []
    for x in raw:
        url = x.get("url", "")
        title = clean(x.get("title", ""))
        if not title or not url or url in seen:
            continue
        seen.add(url)
        x["title"] = title
        candidates.append(x)

    candidates.sort(key=lambda x: priority(x.get("title", "")), reverse=True)

    # Maximal 15 redaktionelle Meldungen. Fehler einer einzelnen Quelle/Meldung stoppen nichts.
    items = []
    for x in candidates[:15]:
        try:
            add_item(items, x)
        except Exception as e:
            print("Meldung übersprungen:", x.get("title", "")[:100], e)
        time.sleep(1)

    # Wichtig: Auch bei 0 Treffern wird eine gültige news.json geschrieben.
    result = {
        "schema_version": "7.0",
        "generated_at": NOW.isoformat(),
        "generated_at_display": NOW.strftime("%d.%m.%Y %H:%M UTC"),
        "sources_checked": {
            "google_news_rss": True,
            "europe_pmc": True,
            "clinicaltrials_gov": True,
            "gdelt": False,
            "pubmed_eutils": False,
        },
        "items": items,
    }

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (ARCHIVE / f"{NOW.strftime('%Y-%m-%d')}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Fertig: {len(items)} Meldungen gespeichert")


if __name__ == "__main__":
    main()

import os
import json
import re
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

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
    "User-Agent": "Forschungsmonitor/10.0 (+https://github.com/Postmen1971/test)",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}
SESSION = requests.Session()
SESSION.headers.update(H)
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8")) if (ROOT / "config.json").exists() else {}
PRIORITY_TERMS = [t.lower() for t in CONFIG.get("usher", {}).get("priority", []) + CONFIG.get("diabetes", {}).get("priority", [])]


def clean(x):
    return re.sub(r"\s+", " ", BeautifulSoup(str(x or ""), "html.parser").get_text(" ", strip=True)).strip()


def gid(title, url):
    return hashlib.sha256((title + "|" + url).encode("utf-8")).hexdigest()[:20]


def get(url, params=None, timeout=40):
    try:
        r = SESSION.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        print("HTTP-Fehler:", url, "->", e)
        return None


def google_news(query, topic):
    r = get("https://news.google.com/rss/search", {"q": query, "hl": "de", "gl": "DE", "ceid": "DE:de"})
    if not r:
        return []
    try:
        soup = BeautifulSoup(r.text, "xml")
        out = []
        for item in soup.find_all("item")[:20]:
            title = clean(item.find("title").get_text() if item.find("title") else "")
            url = clean(item.find("link").get_text() if item.find("link") else "")
            pub = clean(item.find("pubDate").get_text() if item.find("pubDate") else "")
            desc = clean(item.find("description").get_text() if item.find("description") else "")
            source = clean(item.find("source").get_text() if item.find("source") else "Google News")
            if title and url:
                out.append({"title": title, "url": url, "source": source, "published": pub, "body": desc, "topic": topic})
        return out
    except Exception as e:
        print("Google-News-Fehler:", e)
        return []


def europe_pmc(query, topic):
    r = get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", {
        "query": query, "format": "json", "pageSize": 15,
        "sort": "FIRST_PDATE_D desc", "resultType": "core"
    })
    if not r:
        return []
    try:
        out = []
        for x in r.json().get("resultList", {}).get("result", []):
            pmid = str(x.get("pmid") or x.get("id") or "")
            title = clean(x.get("title", ""))
            if not pmid or not title:
                continue
            out.append({
                "title": title,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "source": "PubMed / Europe PMC",
                "published": str(x.get("firstPublicationDate") or x.get("pubYear") or ""),
                "body": clean(x.get("abstractText", "")),
                "topic": topic,
            })
        return out
    except Exception as e:
        print("Europe-PMC-Fehler:", e)
        return []


def clinical_trials(query, topic):
    r = get("https://clinicaltrials.gov/api/v2/studies", {"query.term": query, "pageSize": 20, "format": "json"})
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
            arms = p.get("armsInterventionsModule", {})
            outcomes = p.get("outcomesModule", {})
            eligibility = p.get("eligibilityModule", {})
            nct = ident.get("nctId", "")
            title = clean(ident.get("briefTitle", ""))
            if not nct or not title:
                continue

            interventions = []
            for it in arms.get("interventions", [])[:8]:
                name = clean(it.get("name", ""))
                typ = clean(it.get("type", ""))
                description = clean(it.get("description", ""))
                if name:
                    interventions.append(f"{name} ({typ})" + (f": {description}" if description else ""))

            primary_outcomes = []
            for o in outcomes.get("primaryOutcomes", [])[:8]:
                name = clean(o.get("measure", ""))
                time_frame = clean(o.get("timeFrame", ""))
                if name:
                    primary_outcomes.append(name + (f"; Zeitraum: {time_frame}" if time_frame else ""))

            body_parts = [
                desc.get("briefSummary", ""),
                desc.get("detailedDescription", ""),
                "Studientyp: " + str(design.get("studyType", "")),
                "Phasen: " + ", ".join(design.get("phases", [])),
                "Randomisierung: " + str(design.get("designInfo", {}).get("allocation", "")),
                "Verblindung: " + str(design.get("designInfo", {}).get("maskingInfo", {}).get("masking", "")),
                "Geplante Teilnehmerzahl: " + str(design.get("enrollmentInfo", {}).get("count", "")),
                "Interventionen: " + " | ".join(interventions),
                "Primäre Endpunkte: " + " | ".join(primary_outcomes),
                "Teilnahmevoraussetzungen: " + clean(eligibility.get("eligibilityCriteria", "")),
            ]
            body = clean(" ".join(x for x in body_parts if x and x not in ("Studientyp: ", "Phasen: ", "Randomisierung: ", "Verblindung: ")))
            out.append({
                "title": title,
                "url": f"https://clinicaltrials.gov/study/{nct}",
                "source": "ClinicalTrials.gov",
                "published": status.get("studyFirstPostDateStruct", {}).get("date", ""),
                "body": body,
                "topic": topic,
                "phase": ", ".join(design.get("phases", [])),
                "status": status.get("overallStatus", "")
            })
        return out
    except Exception as e:
        print("ClinicalTrials-Fehler:", e)
        return []


def article_text(url):
    r = get(url, timeout=25)
    if not r:
        return ""
    try:
        s = BeautifulSoup(r.text, "html.parser")
        for x in s(["script", "style", "nav", "footer", "header", "aside", "form"]):
            x.decompose()
        node = s.find("article") or s.find("main") or s.body
        return clean(node.get_text(" ", strip=True) if node else "")[:18000]
    except Exception:
        return ""


def priority(title):
    s = title.lower()
    points = {
        "luce-1": 150, "aavb-081": 150, "aavantgarde": 140,
        "usher 1b": 125, "ush1b": 125, "myo7a": 115,
        "gene therapy": 80, "gene editing": 75, "clinical trial": 65,
        "type 2 diabetes": 50, "glp-1": 45, "gip": 45, "retatrutide": 45
    }
    return sum(v for k, v in points.items() if k in s) + sum(15 for t in PRIORITY_TERMS if t and t in s)


def probably_german(text):
    """Strenge Prüfung, damit englische Sätze nicht als deutsche Ausgabe durchrutschen."""
    s = " " + str(text or "").lower() + " "
    english = [
        " the ", " and ", " is ", " are ", " study ", " patients ", " purpose ",
        " safety ", " efficacy ", " treatment ", " will ", " with ", " this ",
        " following ", " evaluate ", " single ", " injection ", " administered ",
        " participants ", " results ", " randomized ", " placebo ", " primary endpoint "
    ]
    german = [
        " der ", " die ", " das ", " und ", " ist ", " sind ", " studie ",
        " patienten ", " ziel ", " sicherheit ", " wirksamkeit ", " behandlung ",
        " wird ", " mit ", " diese ", " teilnehmer ", " ergebnisse ", " primäre ",
        " injektion ", " endpunkt ", " randomisiert ", " auswertung "
    ]
    e = sum(s.count(w) for w in english)
    g = sum(s.count(w) for w in german)
    return g >= 2 and g >= e


def clinical_title_is_generic(title):
    t = clean(title).lower()
    return not t or t.startswith("forschungsinformation zu") or t in {
        "forschungsinformation zu usher-syndrom typ 1b",
        "forschungsinformation zu typ-2-diabetes"
    }


def gemini_call(prompt, key):
    endpoint = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 3500, "responseMimeType": "application/json"}
    }
    r = requests.post(endpoint, headers={"x-goog-api-key": key, "Content-Type": "application/json"}, json=payload, timeout=120)
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.I).strip()
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        text = text[a:b + 1]
    return json.loads(text)


def translate_to_german(title, source, body, topic, phase, key):
    clinical = source == "ClinicalTrials.gov"
    base = f"""Du bist der deutsche wissenschaftliche Redakteur des Forschungsmonitors.

ABSOLUTE REGELN:
1. title_de, summary_de, detailed_summary_de und why_relevant MÜSSEN vollständig auf Deutsch geschrieben sein.
2. Kein englischer erklärender Satz darf übernommen werden. Übersetze den Inhalt sinngemäß ins Deutsche.
3. Fachbegriffe, Eigennamen, Gennamen, Medikamentennamen, Studiennamen, NCT-Nummern und offizielle Quellenbezeichnungen dürfen unverändert bleiben.
4. Erfinde niemals Ergebnisse, Teilnehmerzahlen, Sicherheitsdaten oder Wirksamkeitsdaten.
5. Wenn etwas im Originalinhalt nicht angegeben ist, schreibe ausdrücklich „nicht angegeben“ oder lasse die Aussage weg.

BESONDERS WICHTIG FÜR CLINICALTRIALS.GOV:
- title_de MUSS eine echte, möglichst genaue deutsche Übersetzung des Originaltitels sein.
- Verwende NICHT „Forschungsinformation zu Usher-Syndrom Typ 1B“ als Ersatz für den Studientitel.
- Übersetze den kompletten Studientitel einschließlich Therapie, Erkrankung und Studienstatus, sofern diese Bestandteile im Originaltitel stehen.
- detailed_summary_de muss den tatsächlich gelieferten Registerinhalt erklären: Ziel, Therapie/Intervention, Studiendesign, Phase, Status, geplante Teilnehmer, Endpunkte und relevante Ein-/Ausschlusskriterien, soweit vorhanden.
- Bei einer laufenden Studie darfst du keine Ergebnisse erfinden. Beschreibe stattdessen, was untersucht und welche Ergebnisse künftig bewertet werden.

Quelle: {source}
Thema: {topic}
Studienphase/Status: {phase}
Originaltitel: {title}

ORIGINALINHALT:
{body[:14000]}

Erstelle eine eigenständige deutsche Zusammenfassung und NICHT nur eine allgemeine Beschreibung der Meldung.

Gib ausschließlich gültiges JSON zurück mit genau diesen Feldern:
"title_de", "summary_de", "detailed_summary_de", "why_relevant", "country", "evidence_key", "study_phase".
"""
    for attempt in range(3):
        try:
            result = gemini_call(base, key)
            fields = ["title_de", "summary_de", "detailed_summary_de", "why_relevant"]
            values = {k: clean(result.get(k, "")) for k in fields}
            valid = all(probably_german(values[k]) for k in fields)
            if clinical:
                valid = valid and not clinical_title_is_generic(values["title_de"])
            if valid:
                return result
            print(f"Gemini-Ausgabe noch nicht ausreichend deutsch/konkret – neuer Versuch {attempt + 1}/3.")
            base += "\n\nFEHLERKORREKTUR: Deine letzte Antwort war nicht ausreichend. Liefere diesmal eine echte deutsche Übersetzung des Originaltitels und eine konkrete deutsche Zusammenfassung des gelieferten Inhalts. Kein englischer Erklärungssatz. Keine generische Überschrift."
        except Exception as e:
            print(f"Gemini-Fehler Versuch {attempt + 1}/3: {e}")
            time.sleep(3)
    return {}


def fallback_german(title, source, topic, phase):
    if source == "ClinicalTrials.gov":
        summary = f"Es handelt sich um eine bei ClinicalTrials.gov registrierte klinische Studie. Der aktuelle Status ist {phase or 'im Studienregister angegeben'}."
        detail = f"Die Studie untersucht einen medizinischen Therapieansatz im Themenbereich {('Usher-Syndrom Typ 1B / MYO7A' if topic == 'usher' else 'Typ-2-Diabetes')}. Die Studie ist bei ClinicalTrials.gov registriert. Der angegebene Entwicklungsstand lautet {phase or 'nicht angegeben'}. Aus den vorliegenden Registerdaten lässt sich ohne weitere Angaben keine Aussage über bereits erzielte Wirksamkeit ableiten. Ergebnisse dürfen bei einer laufenden Studie nicht vorweggenommen werden."
    else:
        summary = "Die Quelle beschreibt eine wissenschaftliche Information aus dem Forschungsbereich. Die wesentlichen Angaben werden auf Grundlage des verfügbaren Originalinhalts zusammengefasst."
        detail = f"Die Meldung betrifft den Forschungsbereich {('Usher-Syndrom Typ 1B / MYO7A' if topic == 'usher' else 'Typ-2-Diabetes')}. Die Zusammenfassung basiert auf dem verfügbaren Inhalt der Quelle {source}. Nicht im verfügbaren Inhalt enthaltene Ergebnisse werden nicht ergänzt. Für eine genaue Bewertung sollte die Originalquelle herangezogen werden."
    return {
        "title_de": "Forschungsinformation zu " + ("Usher-Syndrom Typ 1B" if topic == "usher" else "Typ-2-Diabetes"),
        "summary_de": summary,
        "detailed_summary_de": detail,
        "why_relevant": "Die Meldung ist für den entsprechenden Forschungsbereich relevant.",
        "country": "",
        "evidence_key": "frueh",
        "study_phase": phase or "Nicht angegeben"
    }


def add_item(items, x, key):
    title = clean(x.get("title", "")); url = x.get("url", "")
    if not title or not url:
        return
    source = x.get("source", "Unbekannte Quelle")
    topic = x.get("topic", "usher")
    body = clean(x.get("body", ""))
    phase = x.get("phase", "") or x.get("status", "")
    if source not in ("ClinicalTrials.gov", "PubMed / Europe PMC") and len(body) < 600:
        original = article_text(url)
        if len(original) > len(body):
            body = original
    ai = translate_to_german(title, source, body, topic, phase, key) if body and key else {}
    if not ai:
        ai = fallback_german(title, source, topic, phase)
    title_de = clean(ai.get("title_de", ""))
    summary = clean(ai.get("summary_de", ""))
    detailed = clean(ai.get("detailed_summary_de", ""))
    if not title_de or not probably_german(title_de) or (source == "ClinicalTrials.gov" and clinical_title_is_generic(title_de)):
        title_de = fallback_german(title, source, topic, phase)["title_de"]
    if not summary or not probably_german(summary):
        summary = fallback_german(title, source, topic, phase)["summary_de"]
    if not detailed or not probably_german(detailed):
        detailed = fallback_german(title, source, topic, phase)["detailed_summary_de"]
    evidence = clean(ai.get("evidence_key", "")) or "frueh"
    items.append({
        "id": gid(title, url), "title": title, "title_de": title_de,
        "summary_de": summary, "detailed_summary_de": detailed,
        "why_relevant": clean(ai.get("why_relevant", "")) or "Thematisch relevante Meldung.",
        "url": url, "source": source, "country": clean(ai.get("country", "")),
        "published": x.get("published", ""), "published_display": display_date(x.get("published", "")),
        "topic": topic, "category": "clinicaltrials" if source == "ClinicalTrials.gov" else "forschung",
        "evidence": evidence, "evidence_key": evidence,
        "study_phase": clean(ai.get("study_phase", "")) or phase or "Nicht angegeben",
        "priority": priority(title)
    })


def display_date(value):
    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%d.%m.%Y") if "T" in str(value) else datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return str(value)


def main():
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        print("FEHLER: GEMINI_API_KEY fehlt")
    raw = []
    for q in ['"Usher 1B" OR USH1B OR MYO7A', '"LUCE-1" OR "AAVB-081"', 'AAVantgarde gene therapy', 'Usher syndrome gene therapy']:
        raw.extend(google_news(q, "usher"))
    for q in ['"type 2 diabetes" new treatment', '"type 2 diabetes" clinical trial', '"type 2 diabetes" GLP-1 GIP', 'retatrutide diabetes']:
        raw.extend(google_news(q, "diabetes"))
    raw.extend(europe_pmc('(USH1B OR "Usher 1B" OR MYO7A) AND (gene therapy OR gene editing OR AAV)', "usher"))
    raw.extend(europe_pmc('("type 2 diabetes" OR T2D) AND (GLP-1 OR GIP OR retatrutide OR treatment)', "diabetes"))
    raw.extend(clinical_trials('Usher syndrome type 1B MYO7A', "usher"))
    raw.extend(clinical_trials('type 2 diabetes GLP-1 GIP', "diabetes"))

    seen = set(); candidates = []
    for x in raw:
        u = x.get("url", "")
        if u and u not in seen and clean(x.get("title", "")):
            seen.add(u); candidates.append(x)
    candidates.sort(key=lambda x: priority(x.get("title", "")), reverse=True)

    items = []
    for x in candidates[:30]:
        add_item(items, x, key)
        time.sleep(0.5)

    result = {
        "schema_version": "10.0",
        "generated_at": NOW.isoformat(),
        "generated_at_display": NOW.strftime("%d.%m.%Y %H:%M UTC"),
        "sources_checked": {"google_news_rss": True, "europe_pmc": True, "clinicaltrials_gov": True, "gdelt": False, "pubmed_eutils": False},
        "items": items
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (ARCHIVE / NOW.strftime("%Y-%m-%d.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    german = sum(1 for i in items if probably_german(i["title_de"] + " " + i["summary_de"] + " " + i["detailed_summary_de"]))
    print(f"Fertig: {len(items)} Meldungen; {german}/{len(items)} mit deutscher Ausgabe.")


if __name__ == "__main__":
    main()

import os, json, re, time
from datetime import datetime, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'data' / 'news.json'
ARCHIVE = ROOT / 'data' / 'archive'
ARCHIVE.mkdir(parents=True, exist_ok=True)
NOW = datetime.now(timezone.utc)
KEY = os.getenv('GEMINI_API_KEY', '').strip()

ENGLISH_MARKERS = [
    ' the ', ' and ', ' is ', ' are ', ' study ', ' patients ', ' purpose ',
    ' safety ', ' efficacy ', ' treatment ', ' will ', ' with ', ' this ',
    ' following ', ' evaluate ', ' injection ', ' administered ',
    ' participants ', ' results ', ' randomized ', ' placebo ',
    ' primary endpoint ', ' due to ', ' in patients ', ' gene therapy ',
    ' translational research ', ' mouse models ', ' phase ', ' recruiting ',
]
GERMAN_MARKERS = [
    ' der ', ' die ', ' das ', ' und ', ' ist ', ' sind ', ' studie ',
    ' patient', ' ziel ', ' sicherheit ', ' wirksamkeit ', ' behandlung ',
    ' wird ', ' mit ', ' diese ', ' teilnehmer ', ' ergebnis', ' primär',
    ' injektion ', ' endpunkt ', ' randomisiert ', ' auswertung ', ' quelle ',
    ' daten ', ' meldung ', ' untersucht ', ' beschreibt ', ' zeigt ',
    ' einer ', ' eine ', ' einem ', ' eines ', ' den ', ' dem ', ' für ',
]

def clean(x):
    return re.sub(r'\s+', ' ', BeautifulSoup(str(x or ''), 'html.parser').get_text(' ', strip=True)).strip()

def english_score(text):
    s = ' ' + str(text or '').lower() + ' '
    return sum(s.count(x) for x in ENGLISH_MARKERS)

def german_score(text):
    s = ' ' + str(text or '').lower() + ' '
    return sum(s.count(x) for x in GERMAN_MARKERS)

def is_german(text, title=False):
    text = clean(text)
    if not text:
        return False
    e = english_score(text)
    d = german_score(text)
    if e >= 2 and e > d:
        return False
    if title:
        return d >= 1 or e == 0
    return d >= 2 and e == 0

def needs_repair(item):
    title = clean(item.get('title_de', ''))
    original = clean(item.get('title', ''))
    return (
        not is_german(title, title=True)
        or title == original
        or not is_german(item.get('summary_de', ''))
        or not is_german(item.get('detailed_summary_de', ''))
        or not is_german(item.get('why_relevant', ''))
    )

def clinical_body(url):
    m = re.search(r'/study/(NCT\d+)', url or '', re.I)
    if not m:
        return ''
    try:
        r = requests.get('https://clinicaltrials.gov/api/v2/studies/' + m.group(1), timeout=35, headers={'User-Agent': 'Forschungsmonitor/13.0'})
        r.raise_for_status()
        p = r.json().get('protocolSection', {})
        ident = p.get('identificationModule', {})
        status = p.get('statusModule', {})
        desc = p.get('descriptionModule', {})
        design = p.get('designModule', {})
        arms = p.get('armsInterventionsModule', {})
        outcomes = p.get('outcomesModule', {})
        parts = [
            'Originaltitel: ' + clean(ident.get('officialTitle') or ident.get('briefTitle', '')),
            'Kurzbeschreibung: ' + clean(desc.get('briefSummary', '')),
            'Ausführliche Beschreibung: ' + clean(desc.get('detailedDescription', '')),
            'Status: ' + clean(status.get('overallStatus', '')),
            'Phase: ' + ', '.join(design.get('phases', [])),
            'Studientyp: ' + clean(design.get('studyType', '')),
            'Teilnehmerzahl: ' + str(design.get('enrollmentInfo', {}).get('count', '')),
        ]
        interventions = []
        for it in arms.get('interventions', [])[:10]:
            n = clean(it.get('name', ''))
            d = clean(it.get('description', ''))
            if n:
                interventions.append(n + (': ' + d if d else ''))
        if interventions:
            parts.append('Interventionen: ' + ' | '.join(interventions))
        primary = []
        for o in outcomes.get('primaryOutcomes', [])[:10]:
            n = clean(o.get('measure', ''))
            tf = clean(o.get('timeFrame', ''))
            if n:
                primary.append(n + ('; Zeitraum: ' + tf if tf else ''))
        if primary:
            parts.append('Primäre Endpunkte: ' + ' | '.join(primary))
        return clean(' '.join(parts))[:12000]
    except Exception as e:
        print('ClinicalTrials-Inhalt nicht abrufbar:', e)
        return ''

def pubmed_body(url):
    m = re.search(r'/([0-9]{6,})/?$', url or '')
    if not m:
        return ''
    try:
        r = requests.get('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi', params={'db': 'pubmed', 'id': m.group(1), 'rettype': 'abstract', 'retmode': 'text'}, timeout=35, headers={'User-Agent': 'Forschungsmonitor/13.0'})
        r.raise_for_status()
        return clean(r.text)[:12000]
    except Exception as e:
        print('PubMed-Inhalt nicht abrufbar:', e)
        return ''

def article_body(url):
    try:
        r = requests.get(url, timeout=25, headers={'User-Agent': 'Forschungsmonitor/13.0', 'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8'})
        r.raise_for_status()
        s = BeautifulSoup(r.text, 'html.parser')
        for x in s(['script', 'style', 'nav', 'footer', 'header', 'aside', 'form']):
            x.decompose()
        node = s.find('article') or s.find('main') or s.body
        return clean(node.get_text(' ', strip=True) if node else '')[:12000]
    except Exception:
        return ''

def source_body(item):
    source = clean(item.get('source', ''))
    url = item.get('url', '')
    if source == 'ClinicalTrials.gov':
        body = clinical_body(url)
        if body:
            return body
    if 'PubMed' in source:
        body = pubmed_body(url)
        if body:
            return body
    for key in ('body', 'source_summary', 'detailed_summary_de', 'summary_de'):
        body = clean(item.get(key, ''))
        if len(body) > 80 and not (key.endswith('_de') and english_score(body) == 0):
            return body[:12000]
    return article_body(url) or clean(item.get('title', ''))

def title_fallback(title, topic):
    t = clean(title)
    replacements = [
        (r'\bStudy of\b', 'Studie zu'),
        (r'\bStudy\b', 'Studie'),
        (r'\bSubretinally Injected\b', 'subretinal injiziertem'),
        (r'\bPatients With\b', 'bei Patienten mit'),
        (r'\bPatient[s]? With\b', 'Patienten mit'),
        (r'\bUsher Syndrome Type IB\b', 'Usher-Syndrom Typ 1B'),
        (r'\bUsher Syndrome Type 1B\b', 'Usher-Syndrom Typ 1B'),
        (r'\bRetinitis Pigmentosa\b', 'Retinitis pigmentosa'),
        (r'\bThird-generation\b', 'Dritte Generation'),
        (r'\bMouse models and translational research of\b', 'Mausmodelle und translationale Forschung zu'),
        (r'\bhereditary vestibular dysfunction\b', 'erblichen Gleichgewichtsstörungen'),
        (r'\bviral gene therapy\b', 'viraler Gentherapie'),
        (r'\blentiviral gene therapy\b', 'lentiviraler Gentherapie'),
        (r'\brescues function\b', 'stellt die Funktion wieder her'),
        (r'\btranslational research\b', 'translationale Forschung'),
        (r'\bwith Usher Syndrome\b', 'bei Usher-Syndrom'),
        (r'\bin Patients\b', 'bei Patienten'),
    ]
    out = t
    for pattern, repl in replacements:
        out = re.sub(pattern, repl, out, flags=re.I)
    if out != t:
        return out
    return 'Neue Forschung zu Usher-Syndrom Typ 1B / MYO7A' if topic == 'usher' else 'Neue Forschung zu Typ-2-Diabetes'

def gemini_batch(batch):
    payload_items = []
    for idx, item in enumerate(batch, 1):
        payload_items.append({
            'id': idx,
            'original_title': clean(item.get('title', '')),
            'source': clean(item.get('source', 'Quelle')),
            'topic': clean(item.get('topic', 'usher')),
            'phase_status': clean(item.get('study_phase', '')),
            'original_content': source_body(item),
        })
    prompt = '''Du bist der deutsche wissenschaftliche Redakteur des Forschungsmonitors.

ÜBERSETZE UND REDIGIERE JEDE MELDUNG VOLLSTÄNDIG AUF DEUTSCH.
- title_de MUSS eine konkrete deutsche Überschrift sein und darf nicht einfach der englische Originaltitel sein.
- summary_de, detailed_summary_de und why_relevant müssen vollständig Deutsch sein.
- Eigennamen, Gen-/Medikamentennamen, AAVB-081, LUCE-1, MYO7A, NCT-Nummern und ClinicalTrials.gov dürfen unverändert bleiben.
- Nichts erfinden. Bei fehlenden Angaben „nicht angegeben“ schreiben.
- Bei laufenden Studien keine Wirksamkeit oder Sicherheit erfinden.
- Presseberichte klar als Presse einordnen und sensationelle Aussagen nicht als bewiesene Tatsachen darstellen.
- Übernimm keine englischen Erklärungssätze.

Gib AUSSCHLIESSLICH ein JSON-Array zurück. Für jede Eingabe genau ein Objekt mit id, title_de, summary_de, detailed_summary_de, why_relevant, country, evidence_key und study_phase.

MELDUNGEN:
''' + json.dumps(payload_items, ensure_ascii=False)
    endpoint = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent'
    r = requests.post(endpoint, headers={'x-goog-api-key': KEY, 'Content-Type': 'application/json'}, json={'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': {'temperature': 0.1, 'maxOutputTokens': 7000, 'responseMimeType': 'application/json'}}, timeout=180)
    r.raise_for_status()
    text = r.json()['candidates'][0]['content']['parts'][0]['text'].strip()
    text = re.sub(r'^```json\s*|\s*```$', '', text, flags=re.I).strip()
    a, b = text.find('['), text.rfind(']')
    if a < 0 or b <= a:
        raise ValueError('Gemini lieferte kein JSON-Array')
    result = json.loads(text[a:b + 1])
    return {int(x.get('id')): x for x in result if str(x.get('id', '')).isdigit()}

def main():
    data = json.loads(OUT.read_text(encoding='utf-8'))
    items = data.get('items', [])
    bad_items = [x for x in items if needs_repair(x)]
    repaired = 0
    batches = [bad_items[i:i + 4] for i in range(0, len(bad_items), 4)]
    if not KEY:
        print('WARNUNG: GEMINI_API_KEY fehlt; es wird nur der sichere deutsche Fallback verwendet.')
    for batch_no, batch in enumerate(batches, 1):
        results = {}
        if KEY:
            try:
                results = gemini_batch(batch)
                print(f'Gemini-Batch {batch_no}/{len(batches)} erfolgreich: {len(results)} Meldungen.')
            except Exception as e:
                print(f'Gemini-Batch {batch_no}/{len(batches)} fehlgeschlagen:', e)
        for idx, item in enumerate(batch, 1):
            x = results.get(idx, {})
            if x:
                fields = ['title_de', 'summary_de', 'detailed_summary_de', 'why_relevant', 'country', 'evidence_key', 'study_phase']
                for field in fields:
                    value = clean(x.get(field, ''))
                    if value:
                        item[field] = value
                if is_german(item.get('title_de'), True) and is_german(item.get('summary_de')) and is_german(item.get('detailed_summary_de')) and is_german(item.get('why_relevant')):
                    repaired += 1
                    continue
            if not is_german(item.get('title_de'), True) or clean(item.get('title_de')) == clean(item.get('title')):
                item['title_de'] = title_fallback(item.get('title', ''), item.get('topic', 'usher'))
            if not is_german(item.get('summary_de')):
                item['summary_de'] = 'Die Meldung betrifft den Forschungsbereich ' + ('Usher-Syndrom Typ 1B / MYO7A.' if item.get('topic') == 'usher' else 'Typ-2-Diabetes.')
            if not is_german(item.get('detailed_summary_de')):
                item['detailed_summary_de'] = 'Die verfügbaren Angaben werden vorsichtig zusammengefasst. Nicht im Originalinhalt enthaltene Ergebnisse werden nicht ergänzt.'
            if not is_german(item.get('why_relevant')):
                item['why_relevant'] = 'Die Meldung ist für den entsprechenden Forschungsbereich relevant.'
        time.sleep(2)
    data['schema_version'] = '13.0'
    data['generated_at'] = NOW.isoformat()
    data['generated_at_display'] = NOW.strftime('%d.%m.%Y %H:%M UTC')
    data['full_refresh'] = True
    data['translation_check'] = {'checked': len(items), 'repaired': repaired, 'language': 'de', 'source_content_refetched': True, 'gemini_batches': len(batches)}
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    (ARCHIVE / NOW.strftime('%Y-%m-%d-%H%M%S.json')).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Kompletter Sprachcheck: {len(items)} Meldungen geprüft, {repaired} erfolgreich deutsch repariert.')

if __name__ == '__main__':
    main()

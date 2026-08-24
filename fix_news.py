import os, json, re, time
from datetime import datetime, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'data'/'news.json'; ARCHIVE=ROOT/'data'/'archive'; ARCHIVE.mkdir(parents=True,exist_ok=True)
NOW=datetime.now(timezone.utc); KEY=os.getenv('GEMINI_API_KEY','')
EN=[" the "," and "," is "," are "," study "," patients "," purpose "," safety "," efficacy "," treatment "," will "," with "," this "," following "," evaluate "," injection "," administered "," participants "," results "," randomized "," placebo "," primary endpoint "," due to "," in patients "," gene therapy "]
DE=[" der "," die "," das "," und "," ist "," sind "," studie "," patient", " ziel "," sicherheit "," wirksamkeit "," behandlung "," wird "," mit "," diese "," teilnehmer "," ergebnis", " primär", " injektion "," endpunkt "," randomisiert "," auswertung "," quelle "," daten "," meldung "," untersucht "," beschreibt "," zeigt "," einer "]

def clean(x): return re.sub(r'\s+',' ',BeautifulSoup(str(x or ''),'html.parser').get_text(' ',strip=True)).strip()
def german(s,title=False):
    s=' '+str(s or '').lower()+' '; e=sum(s.count(x) for x in EN); d=sum(s.count(x) for x in DE)
    if e>=2: return False
    return (d>=1 if title else d>=2 and e==0)
def fallback(topic): return 'Neue Forschung zu Usher-Syndrom Typ 1B / MYO7A' if topic=='usher' else 'Neue Forschung zu Typ-2-Diabetes'

def gemini(prompt):
    r=requests.post('https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent',headers={'x-goog-api-key':KEY,'Content-Type':'application/json'},json={'contents':[{'parts':[{'text':prompt}]}],'generationConfig':{'temperature':0.1,'maxOutputTokens':3200,'responseMimeType':'application/json'}},timeout=120)
    r.raise_for_status(); t=r.json()['candidates'][0]['content']['parts'][0]['text'].strip(); t=re.sub(r'^```json\s*|\s*```$','',t,flags=re.I).strip(); a,b=t.find('{'),t.rfind('}');
    if a<0 or b<=a: raise ValueError('Kein JSON von Gemini')
    return json.loads(t[a:b+1])

def translate(item):
    title=clean(item.get('title','')); topic=item.get('topic','usher'); source=item.get('source','Quelle'); phase=item.get('study_phase','')
    body=clean(item.get('detailed_summary_de') or item.get('summary_de') or title)
    prompt=f'''Du bist deutscher wissenschaftlicher Redakteur. ALLE Felder title_de, summary_de, detailed_summary_de und why_relevant MÜSSEN vollständig Deutsch sein. Übersetze englische Sätze vollständig. Fachnamen wie MYO7A, AAVB-081, LUCE-1, Medikamentennamen, NCT-Nummern und ClinicalTrials.gov dürfen unverändert bleiben. Nichts erfinden; fehlende Angaben als „nicht angegeben“ kennzeichnen. Bei Presseberichten Behauptung und wissenschaftlichen Beweis klar trennen.
Originaltitel: {title}\nQuelle: {source}\nThema: {topic}\nPhase/Status: {phase}\nVorhandener Inhalt: {body[:12000]}
Gib ausschließlich JSON zurück: {{"title_de":"...","summary_de":"...","detailed_summary_de":"...","why_relevant":"...","country":"...","evidence_key":"...","study_phase":"..."}}'''
    for _ in range(3):
        try:
            x=gemini(prompt)
            if german(x.get('title_de'),True) and all(german(x.get(k,'')) for k in ['summary_de','detailed_summary_de','why_relevant']): return x
        except Exception as e: print('Gemini-Fehler:',e)
        time.sleep(2)
    return None

def main():
    data=json.loads(OUT.read_text(encoding='utf-8')); items=data.get('items',[]); repaired=0
    for item in items:
        bad=(not german(item.get('title_de'),True) or not german(item.get('summary_de')) or not german(item.get('detailed_summary_de')) or item.get('title_de','').strip()==item.get('title','').strip())
        if bad and KEY:
            x=translate(item)
            if x:
                for k in ['title_de','summary_de','detailed_summary_de','why_relevant','country','evidence_key','study_phase']:
                    if clean(x.get(k,'')): item[k]=clean(x[k])
                repaired+=1
        if not german(item.get('title_de'),True): item['title_de']=fallback(item.get('topic'))
        if not german(item.get('summary_de')): item['summary_de']='Für diese Meldung konnte keine verlässliche deutsche Kurzfassung erstellt werden. Bitte die Originalquelle prüfen.'
        if not german(item.get('detailed_summary_de')): item['detailed_summary_de']='Für diese Meldung konnte keine verlässliche deutsche Detailzusammenfassung erstellt werden. Bitte die Originalquelle prüfen.'
        if not german(item.get('why_relevant')): item['why_relevant']='Die Meldung ist für den entsprechenden Forschungsbereich relevant.'
    data['schema_version']='12.0'; data['generated_at']=NOW.isoformat(); data['generated_at_display']=NOW.strftime('%d.%m.%Y %H:%M UTC'); data['full_refresh']=True; data['translation_check']={'checked':len(items),'repaired':repaired,'language':'de'}
    OUT.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    (ARCHIVE/NOW.strftime('%Y-%m-%d-%H%M%S.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Kompletter Sprachcheck: {len(items)} Meldungen geprüft, {repaired} repariert.')

if __name__=='__main__': main()

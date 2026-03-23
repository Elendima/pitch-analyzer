#!/usr/bin/env python3
"""
Pitch Deck Analyzer
Analizza un pitch deck in PDF, arricchisce con ricerca web, e genera un report HTML.

Uso:
    python analyze_pitch.py deck.pdf
    python analyze_pitch.py deck.pdf --output report.html
    python analyze_pitch.py deck.pdf --no-open
"""

import argparse
import json
import re
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import pdfplumber
import requests
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
from openai import OpenAI

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Sei un partner di un fondo di venture capital con 15 anni di esperienza,
specializzato in investimenti early-stage in Europa e a livello globale.
Hai valutato migliaia di pitch deck e sai esattamente dove i founder mentono per omissione,
dove i numeri non tornano, e dove un'opportunità è genuinamente interessante.

Regole assolute:
- Rispondi ESCLUSIVAMENTE in italiano. Zero parole in inglese, nemmeno nei termini tecnici
  (usa "catena del valore" non "value chain", "quota di mercato" non "market share", ecc.)
- Sii specifico e concreto. Zero frasi generiche. Ogni affermazione deve essere ancorata
  a dati o fatti presenti nel documento o nelle fonti web.
- Se un'informazione non è nel documento né nel web, scrivilo esplicitamente.
- Non inventare nulla."""

PHASE1_PROMPT = """Dal testo del pitch deck qui sotto, estrai SOLO queste informazioni in JSON.
Non aggiungere altro testo.

{
  "nome_azienda": "...",
  "sito_web": "url se presente, altrimenti null",
  "settore": "settore principale in italiano",
  "descrizione_breve": "max 2 righe su cosa fa l'azienda"
}

Testo:
"""

PHASE2_PROMPT = """Sei un analista VC che ha appena letto il pitch deck di {nome_azienda}
e ha trovato informazioni aggiuntive sul web.

Hai a disposizione:
1. TESTO DEL PITCH DECK:
{testo_deck}

2. INFORMAZIONI DAL WEB:
{contesto_web}

Produci un'analisi approfondita in JSON con la struttura esatta qui sotto.
Non aggiungere testo fuori dal JSON. Tutto in italiano.

{{
  "nome_azienda": "...",
  "tagline": "tagline ufficiale se presente, altrimenti null",

  "business": {{
    "problema": "Descrizione precisa del problema. Chi lo sente? Con quale intensità? Quali sono le soluzioni esistenti e perché non bastano?",
    "soluzione": "Come risolve il problema in modo specifico. Qual è il meccanismo chiave?",
    "modello_di_business": "Come genera ricavi. Struttura dei prezzi se nota. Natura dei ricavi (ricorrenti/transazionali). Chi paga e chi usa il prodotto (se diversi)."
  }},

  "prodotto_tecnologia": {{
    "descrizione": "Descrizione funzionale del prodotto. Cosa fa concretamente un utente con questo strumento?",
    "caratteristiche_chiave": ["caratteristica 1", "caratteristica 2", "..."],
    "stack_tecnologico": "Tecnologie usate se menzionate. Se non dichiarate, indica 'Non dichiarato nel deck'.",
    "differenziatore_tecnologico": "C'è un vero moat tecnologico? Brevetti, dati proprietari, algoritmi proprietari? O è execution play?",
    "stadio_di_sviluppo": "Uno tra: pre-prodotto / MVP / beta privata / prodotto lanciato / ricavi attivi / profittevole"
  }},

  "team": {{
    "fondatori": [
      {{
        "nome": "...",
        "ruolo": "...",
        "background": "Esperienze rilevanti specifiche. Dove ha lavorato, cosa ha costruito, perché è la persona giusta per questo problema."
      }}
    ],
    "valutazione_team": "Analisi critica: il team ha domain expertise? Ha già lavorato insieme? Mancano profili chiave (es. CTO, commerciale)? È un team da Serie A?"
  }},

  "mercato": {{
    "settore": "Settore principale",
    "sottosettore": "Verticale o nicchia specifica",
    "dimensione_mercato": "TAM/SAM/SOM se dichiarati con fonte. Se non dichiarati, stima qualitativa motivata.",
    "tasso_di_crescita": "CAGR o trend se dichiarato o stimabile",
    "struttura_della_catena_del_valore": "Descrivi la catena del valore del settore identificando TUTTE le fasi e i tipi di attori in ogni fase. Esempio: 'Il mercato X è composto da: (1) Fornitori di dati grezzi [...], (2) Piattaforme di elaborazione [...], (3) Distributori [...], (4) Clienti finali [...]'",
    "posizionamento_nella_catena": "In quale fase/i si inserisce l'azienda? Da chi riceve input (dati, flussi, clienti)? A chi vende? È un enabler (vende a player B2B della catena) o un operatore (serve il cliente finale)? Presidia una sola fase o più?",
    "dipendenze_strategiche": "Da quali player/piattaforme/dati dipende? Qual è il rischio se quel player cambia le condizioni?",
    "driver_di_mercato": "Quali macro-trend o regolatori stanno creando il momento giusto per questa soluzione?"
  }},

  "competizione": {{
    "player_globali": [
      {{"nome": "...", "descrizione": "Cosa fanno, quanto sono grandi, come si sovrappongono con questa società, in cosa differiscono"}}
    ],
    "player_europei": [
      {{"nome": "...", "descrizione": "Cosa fanno, quanto sono grandi, come si sovrappongono con questa società, in cosa differiscono"}}
    ],
    "vantaggio_competitivo_dichiarato": "Cosa dice il founder che li differenzia",
    "valutazione_critica_del_vantaggio": "Il vantaggio è reale e difendibile? È temporaneo o strutturale? Quanto è difficile da replicare per un player con più risorse?"
  }},

  "domande_per_il_founder": [
    "Domanda 1: [su unit economics o metriche chiave - specifica, non rispondibile con sì/no]",
    "Domanda 2: [su go-to-market e acquisizione clienti - cosa è già stato testato?]",
    "Domanda 3: [su un punto critico o contraddizione emersa dal deck]",
    "Domanda 4: [sul moat: perché tra 3 anni un player grande non fa la stessa cosa?]",
    "Domanda 5: [sul team: il gap più evidente o la scelta più rischiosa]",
    "Domanda 6: [sulla struttura del mercato o le dipendenze identificate]",
    "Domanda 7: [sulla visione a lungo termine: dove vuole arrivare e perché è il momento giusto]"
  ],

  "punti_di_attenzione": [
    {{
      "area": "es. Competizione / Team / Tecnologia / Mercato / Financials / Regolatorio / ecc.",
      "gravità": "Alta / Media / Bassa",
      "descrizione": "Descrizione precisa del rischio o della lacuna. Perché è un problema. Cosa dovrebbe chiarire o dimostrare il founder per mitigarlo."
    }}
  ],

  "sintesi": "3-4 frasi che un partner VC direbbe al suo team dopo aver letto il deck: perché potrebbe essere interessante, quali sono i 2 rischi principali, e qual è la domanda a cui bisogna rispondere prima di procedere."
}}"""

# ---------------------------------------------------------------------------
# HTML template  (Lovable-inspired design + confronto societá)
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{nome_azienda} — Analisi Pitch</title>
<style>
  /* ── Reset & base ── */
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg:       #fcfbf8;
    --surface:  #ffffff;
    --border:   #e8e3da;
    --text:     #1c1917;
    --text-2:   #57534e;
    --text-3:   #a8a29e;
    --accent:   #18181b;
    --red:      #dc2626;
    --red-bg:   #fef2f2;
    --red-br:   #fecaca;
    --orange:   #c2410c;
    --orange-bg:#fff7ed;
    --orange-br:#fed7aa;
    --green:    #15803d;
    --green-bg: #f0fdf4;
    --blue:     #1d4ed8;
    --blue-bg:  #eff6ff;
    --r:        14px;
    --r-sm:     8px;
    --shadow:   0 1px 4px rgba(0,0,0,.06), 0 0 0 1px rgba(0,0,0,.04);
  }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
    font-size: 14px; line-height: 1.65; color: var(--text);
    background: var(--bg); padding: 32px 16px 80px;
  }}
  .wrap {{ max-width: 940px; margin: 0 auto; }}

  /* ── Header ── */
  .hd {{
    padding: 40px 0 28px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 28px;
  }}
  .hd-eyebrow {{
    font-size: 11px; font-weight: 600; letter-spacing: .08em;
    text-transform: uppercase; color: var(--text-3); margin-bottom: 10px;
  }}
  .hd h1 {{
    font-size: 36px; font-weight: 700; letter-spacing: -.8px;
    color: var(--text); line-height: 1.15; margin-bottom: 8px;
  }}
  .hd-tagline {{
    font-size: 16px; color: var(--text-2); margin-bottom: 16px; font-style: italic;
  }}
  .hd-meta {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
  .pill {{
    display: inline-flex; align-items: center; gap: 5px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 99px; padding: 3px 12px;
    font-size: 12px; font-weight: 500; color: var(--text-2);
  }}
  .pill.teal {{ background:#f0fdfa; border-color:#99f6e4; color:#0f766e; }}

  /* ── Synthesis ── */
  .synthesis {{
    background: var(--accent); color: #fff;
    border-radius: var(--r); padding: 28px 30px; margin-bottom: 20px;
  }}
  .synthesis .s-label {{
    font-size: 10px; font-weight: 700; letter-spacing: .1em;
    text-transform: uppercase; color: rgba(255,255,255,.45); margin-bottom: 10px;
  }}
  .synthesis p {{ font-size: 15px; line-height: 1.8; color: rgba(255,255,255,.9); }}

  /* ── Cards ── */
  .card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--r); padding: 26px 28px; margin-bottom: 16px;
    box-shadow: var(--shadow);
  }}
  .card-title {{
    font-size: 10px; font-weight: 700; letter-spacing: .1em;
    text-transform: uppercase; color: var(--text-3);
    padding-bottom: 14px; border-bottom: 1px solid var(--border); margin-bottom: 20px;
  }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
  @media (max-width:640px) {{ .grid2 {{ grid-template-columns: 1fr; }} }}

  /* ── Fields ── */
  .field {{ margin-bottom: 20px; }}
  .field:last-child {{ margin-bottom: 0; }}
  .label {{
    font-size: 10px; font-weight: 700; letter-spacing: .07em;
    text-transform: uppercase; color: var(--text-3); margin-bottom: 6px;
  }}
  .value {{ font-size: 14px; color: var(--text-2); line-height: 1.7; }}

  /* ── Tags ── */
  .tag {{
    display: inline-block; border-radius: 6px;
    padding: 2px 10px; font-size: 12px; font-weight: 600; margin: 2px;
  }}
  .tag-blue  {{ background: var(--blue-bg);   color: var(--blue);   }}
  .tag-green {{ background: var(--green-bg);  color: var(--green);  }}
  .tag-orange{{ background: var(--orange-bg); color: var(--orange); }}
  .tag-gray  {{ background: #f4f4f5; color: #52525b; }}

  /* ── Team ── */
  .team-card {{
    background: var(--bg); border: 1px solid var(--border);
    border-radius: var(--r-sm); padding: 16px; margin-bottom: 10px;
  }}
  .team-card:last-child {{ margin-bottom: 0; }}
  .t-name {{ font-weight: 700; font-size: 14px; margin-bottom: 2px; }}
  .t-role  {{ font-size: 12px; color: var(--text-3); margin-bottom: 8px; }}
  .t-bg    {{ font-size: 13px; color: var(--text-2); line-height: 1.6; }}

  /* ── Competitors ── */
  .comp-grid {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 16px;
  }}
  @media (max-width:640px) {{ .comp-grid {{ grid-template-columns: 1fr; }} }}
  .comp-card {{
    background: var(--bg); border: 1px solid var(--border);
    border-radius: var(--r-sm); padding: 14px;
  }}
  .c-badge {{
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .06em; color: var(--text-3); margin-bottom: 5px;
  }}
  .c-name {{ font-weight: 700; font-size: 14px; margin-bottom: 6px; }}
  .c-desc  {{ font-size: 13px; color: var(--text-2); line-height: 1.55; }}

  /* ── Questions ── */
  .q-list {{ list-style: none; counter-reset: q; }}
  .q-list li {{
    counter-increment: q; display: flex; gap: 14px;
    padding: 13px 0; border-bottom: 1px solid var(--border); align-items: flex-start;
  }}
  .q-list li:last-child {{ border-bottom: none; }}
  .q-list li::before {{
    content: counter(q);
    background: var(--accent); color: #fff;
    font-size: 11px; font-weight: 700; min-width: 22px; height: 22px;
    border-radius: 50%; display: flex; align-items: center;
    justify-content: center; flex-shrink: 0; margin-top: 1px;
  }}

  /* ── Flags ── */
  .flag {{
    border-radius: var(--r-sm); padding: 14px 16px; margin-bottom: 10px;
    background: var(--orange-bg); border: 1px solid var(--orange-br);
  }}
  .flag.alta {{ background: var(--red-bg); border-color: var(--red-br); }}
  .flag:last-child {{ margin-bottom: 0; }}
  .flag-hd {{ display: flex; align-items: center; gap: 8px; margin-bottom: 7px; }}
  .f-area {{
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .06em; color: var(--orange);
  }}
  .flag.alta .f-area {{ color: var(--red); }}
  .f-sev {{
    font-size: 10px; font-weight: 700; padding: 2px 8px;
    border-radius: 4px; background: var(--orange-br); color: var(--orange);
  }}
  .flag.alta .f-sev {{ background: var(--red-br); color: var(--red); }}
  .f-desc {{ font-size: 13px; color: var(--text-2); line-height: 1.6; }}

  /* ── Footer ── */
  .footer {{
    text-align: center; color: var(--text-3); font-size: 12px; margin-top: 32px;
  }}

</style>
</head>
<body>
<div class="wrap">

  <!-- Header -->
  <div class="hd">
    <div class="hd-eyebrow">Analisi Pitch Deck</div>
    <h1>{nome_azienda}</h1>
    {tagline_html}
    <div class="hd-meta">
      <span class="pill">{data}</span>
      <span class="pill">GPT-4o</span>
      {web_badge}
    </div>
  </div>

  <!-- Sintesi -->
  <div class="synthesis">
    <div class="s-label">Sintesi del Partner</div>
    <p>{sintesi}</p>
  </div>

  <!-- Business -->
  <div class="card">
    <div class="card-title">Business</div>
    <div class="field">
      <div class="label">Problema</div>
      <div class="value">{problema}</div>
    </div>
    <div class="field">
      <div class="label">Soluzione</div>
      <div class="value">{soluzione}</div>
    </div>
    <div class="field">
      <div class="label">Modello di business</div>
      <div class="value">{modello_di_business}</div>
    </div>
  </div>

  <!-- Prodotto -->
  <div class="card">
    <div class="card-title">Prodotto e Tecnologia</div>
    <div class="field">
      <div class="label">Descrizione</div>
      <div class="value">{descrizione_prodotto}</div>
    </div>
    <div class="field">
      <div class="label">Caratteristiche chiave</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:4px">{features_html}</div>
    </div>
    <div class="grid2">
      <div class="field">
        <div class="label">Stack tecnologico</div>
        <div class="value">{stack_tecnologico}</div>
      </div>
      <div class="field">
        <div class="label">Stadio di sviluppo</div>
        <div class="value"><span class="tag tag-orange">{stadio}</span></div>
      </div>
    </div>
    <div class="field">
      <div class="label">Differenziatore tecnologico</div>
      <div class="value">{differenziatore_tecnologico}</div>
    </div>
  </div>

  <!-- Team -->
  <div class="card">
    <div class="card-title">Team</div>
    {team_html}
    <div class="field" style="margin-top:16px">
      <div class="label">Valutazione critica</div>
      <div class="value">{valutazione_team}</div>
    </div>
  </div>

  <!-- Mercato -->
  <div class="card">
    <div class="card-title">Mercato</div>
    <div class="grid2">
      <div>
        <div class="field">
          <div class="label">Settore</div>
          <div class="value">
            <span class="tag tag-blue">{settore}</span>
            <span class="tag tag-gray">{sottosettore}</span>
          </div>
        </div>
        <div class="field">
          <div class="label">Dimensione mercato</div>
          <div class="value">{dimensione_mercato}</div>
        </div>
        <div class="field">
          <div class="label">Tasso di crescita</div>
          <div class="value">{tasso_di_crescita}</div>
        </div>
        <div class="field">
          <div class="label">Driver di mercato</div>
          <div class="value">{driver_di_mercato}</div>
        </div>
      </div>
      <div>
        <div class="field">
          <div class="label">Struttura della catena del valore</div>
          <div class="value">{struttura_catena}</div>
        </div>
        <div class="field">
          <div class="label">Posizionamento nella catena</div>
          <div class="value">{posizionamento_catena}</div>
        </div>
        <div class="field">
          <div class="label">Dipendenze strategiche</div>
          <div class="value">{dipendenze}</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Competizione -->
  <div class="card">
    <div class="card-title">Arena Competitiva</div>
    <div class="field">
      <div class="label">Vantaggio dichiarato dal founder</div>
      <div class="value">{vantaggio_dichiarato}</div>
    </div>
    <div class="field">
      <div class="label">Valutazione critica</div>
      <div class="value">{valutazione_vantaggio}</div>
    </div>
    {competitors_section}
  </div>

  <!-- Domande -->
  <div class="card">
    <div class="card-title">Domande per il Founder</div>
    <ol class="q-list">
      {questions_html}
    </ol>
  </div>

  <!-- Flags -->
  <div class="card">
    <div class="card-title">Punti di Attenzione</div>
    {flags_html}
  </div>

  <div class="footer">pitch-analyzer · {pdf_filename}</div>

</div>
</body>
</html>"""

# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def extract_text_from_pdf(pdf_path: Path) -> str:
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text()
            if text and text.strip():
                pages.append(f"[Slide {i+1}]\n{text.strip()}")
    return "\n\n".join(pages)

# ---------------------------------------------------------------------------
# Web research
# ---------------------------------------------------------------------------

def scrape_url(url: str, max_chars: int = 3000) -> str:
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=8)
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        return text[:max_chars]
    except Exception:
        return ""


def web_research(nome_azienda: str, sito_web: str, settore: str) -> str:
    results = []

    # 1. Cerca il sito ufficiale se disponibile
    if sito_web:
        print(f"  → Scraping sito: {sito_web}")
        content = scrape_url(sito_web)
        if content:
            results.append(f"SITO UFFICIALE ({sito_web}):\n{content}")

    # 2. DuckDuckGo search
    queries = [
        f"{nome_azienda} startup {settore}",
        f"{nome_azienda} funding investors",
    ]
    try:
        with DDGS() as ddgs:
            for q in queries:
                hits = list(ddgs.text(q, max_results=3))
                for h in hits:
                    snippet = f"[{h.get('title','')}] {h.get('body','')}"
                    results.append(snippet)
    except Exception as e:
        print(f"  → Ricerca web non disponibile: {e}")

    if not results:
        return "Nessuna informazione aggiuntiva trovata sul web."

    return "\n\n".join(results)

# ---------------------------------------------------------------------------
# AI calls
# ---------------------------------------------------------------------------

def phase1_extract(client: OpenAI, pdf_text: str) -> dict:
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Estrai i dati richiesti dal testo. Rispondi solo con JSON valido."},
            {"role": "user", "content": PHASE1_PROMPT + pdf_text[:4000]},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


def phase2_analyze(client: OpenAI, nome: str, pdf_text: str, web_ctx: str) -> dict:
    prompt = PHASE2_PROMPT.format(
        nome_azienda=nome,
        testo_deck=pdf_text,
        contesto_web=web_ctx,
    )
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
        max_tokens=4000,
    )
    return json.loads(resp.choices[0].message.content)

# ---------------------------------------------------------------------------
# HTML render
# ---------------------------------------------------------------------------

def render_html(data: dict, pdf_filename: str, web_used: bool) -> str:
    nome = data.get("nome_azienda", "Startup")
    tagline = data.get("tagline") or ""
    tagline_html = f'<div class="hd-tagline">"{tagline}"</div>' if tagline else ""
    web_badge = '<span class="pill teal">+ ricerca web</span>' if web_used else ""

    biz = data.get("business", {})
    prod = data.get("prodotto_tecnologia", {})
    team_data = data.get("team", {})
    mkt = data.get("mercato", {})
    comp = data.get("competizione", {})

    features = prod.get("caratteristiche_chiave", [])
    features_html = "".join(f'<span class="tag tag-green">{f}</span>' for f in features)

    team_html = ""
    for p in team_data.get("fondatori", []):
        team_html += f"""<div class="team-card">
          <div class="t-name">{p.get('nome','')}</div>
          <div class="t-role">{p.get('ruolo','')}</div>
          <div class="t-bg">{p.get('background','')}</div>
        </div>"""

    def comp_cards(players, label):
        return "".join(f"""<div class="comp-card">
          <div class="c-badge">{label}</div>
          <div class="c-name">{c.get('nome','')}</div>
          <div class="c-desc">{c.get('descrizione','')}</div>
        </div>""" for c in players)

    all_cards = comp_cards(comp.get("player_globali", []), "Globale") + \
                comp_cards(comp.get("player_europei", []), "Europeo")
    competitors_section = f'<div class="comp-grid">{all_cards}</div>' if all_cards else ""

    questions_html = "".join(f"<li>{q}</li>" for q in data.get("domande_per_il_founder", []))

    flags_html = ""
    for f in data.get("punti_di_attenzione", []):
        gravita = f.get("gravità", f.get("gravita", "Media"))
        css_class = "alta" if gravita.lower() == "alta" else ""
        flags_html += f"""<div class="flag {css_class}">
          <div class="flag-hd">
            <span class="f-area">{f.get('area','')}</span>
            <span class="f-sev">{gravita}</span>
          </div>
          <div class="f-desc">{f.get('descrizione','')}</div>
        </div>"""
    if not flags_html:
        flags_html = '<p style="color:var(--text-3)">Nessun punto critico identificato.</p>'

    return HTML_TEMPLATE.format(
        nome_azienda=nome,
        tagline_html=tagline_html,
        data=datetime.now().strftime("%d %B %Y"),
        web_badge=web_badge,
        sintesi=data.get("sintesi", ""),
        problema=biz.get("problema", ""),
        soluzione=biz.get("soluzione", ""),
        modello_di_business=biz.get("modello_di_business", ""),
        descrizione_prodotto=prod.get("descrizione", ""),
        features_html=features_html,
        stack_tecnologico=prod.get("stack_tecnologico", "Non dichiarato"),
        stadio=prod.get("stadio_di_sviluppo", ""),
        differenziatore_tecnologico=prod.get("differenziatore_tecnologico", ""),
        team_html=team_html,
        valutazione_team=team_data.get("valutazione_team", ""),
        settore=mkt.get("settore", ""),
        sottosettore=mkt.get("sottosettore", ""),
        dimensione_mercato=mkt.get("dimensione_mercato", ""),
        tasso_di_crescita=mkt.get("tasso_di_crescita", ""),
        driver_di_mercato=mkt.get("driver_di_mercato", ""),
        struttura_catena=mkt.get("struttura_della_catena_del_valore", ""),
        posizionamento_catena=mkt.get("posizionamento_nella_catena", ""),
        dipendenze=mkt.get("dipendenze_strategiche", ""),
        vantaggio_dichiarato=comp.get("vantaggio_competitivo_dichiarato", ""),
        valutazione_vantaggio=comp.get("valutazione_critica_del_vantaggio", ""),
        competitors_section=competitors_section,
        questions_html=questions_html,
        flags_html=flags_html,
        pdf_filename=pdf_filename,
    )

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Analizza un pitch deck PDF.")
    parser.add_argument("pdf", help="Percorso al PDF")
    parser.add_argument("--output", "-o", help="Percorso output HTML")
    parser.add_argument("--no-open", action="store_true", help="Non aprire il browser")
    parser.add_argument("--no-web", action="store_true", help="Salta la ricerca web")
    args = parser.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        print(f"Errore: file non trovato: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output) if args.output else \
        pdf_path.parent / (pdf_path.stem + "_analisi.html")

    print(f"Carico PDF: {pdf_path.name} ({pdf_path.stat().st_size / 1024:.0f} KB)")
    pdf_text = extract_text_from_pdf(pdf_path)

    if not pdf_text.strip():
        print("Errore: nessun testo estratto. Il PDF potrebbe essere composto solo da immagini.", file=sys.stderr)
        sys.exit(1)

    print(f"  → {len(pdf_text)} caratteri estratti da {pdf_text.count('[Slide')} slide")

    client = OpenAI()

    print("Estraggo informazioni base...")
    meta = phase1_extract(client, pdf_text)
    nome = meta.get("nome_azienda", "Startup")
    sito = meta.get("sito_web") or ""
    settore = meta.get("settore", "")
    print(f"  → Azienda: {nome} | Settore: {settore}")

    web_ctx = ""
    web_used = False
    if not args.no_web:
        print("Ricerca informazioni sul web...")
        web_ctx = web_research(nome, sito, settore)
        web_used = bool(web_ctx and "Nessuna" not in web_ctx)

    print("Analisi approfondita con GPT-4o...")
    data = phase2_analyze(client, nome, pdf_text, web_ctx)
    print("  → Analisi completata.")

    html = render_html(data, pdf_path.name, web_used)
    output_path.write_text(html, encoding="utf-8")
    print(f"Report salvato: {output_path}")

    if not args.no_open:
        webbrowser.open(output_path.as_uri())

    print("\n" + "─" * 60)
    print(f"  Azienda  : {data.get('nome_azienda', 'N/A')}")
    print(f"  Settore  : {data.get('mercato', {}).get('settore', 'N/A')}")
    print(f"  Stadio   : {data.get('prodotto_tecnologia', {}).get('stadio_di_sviluppo', 'N/A')}")
    print(f"  Web      : {'sì' if web_used else 'no'}")
    print(f"  Report   : {output_path}")
    print("─" * 60)


if __name__ == "__main__":
    main()

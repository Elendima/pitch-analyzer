#!/usr/bin/env python3
"""
Pitch Deck Analyzer
Analizza un pitch deck in PDF e genera un report strutturato in HTML.

Uso:
    python analyze_pitch.py deck.pdf
    python analyze_pitch.py deck.pdf --output report.html
    python analyze_pitch.py deck.pdf --no-open   # non apre il browser
"""

import argparse
import base64
import json
import re
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import anthropic

# ---------------------------------------------------------------------------
# Prompt di analisi
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Sei un analista di venture capital esperto, con profonda conoscenza
dell'ecosistema startup europeo e globale. Analizzi pitch deck con occhio critico ma
costruttivo, identificando punti di forza, debolezze e potenziale di investimento.

Rispondi SEMPRE in italiano. Sii specifico, concreto e basati sui contenuti del documento.
Se un'informazione non è presente nel documento, indicalo esplicitamente invece di inventare."""

ANALYSIS_PROMPT = """Analizza questo pitch deck e restituisci un JSON con la seguente struttura esatta.
Non aggiungere testo fuori dal JSON.

{
  "company_name": "nome dell'azienda",
  "tagline": "tagline o pitch in una riga (se presente)",
  "business_description": {
    "problema": "descrizione del problema di mercato che si vuole risolvere",
    "soluzione": "come l'azienda risolve il problema",
    "modello_di_business": "come genera o genera fatturato (es. SaaS, marketplace, B2B, ecc.)"
  },
  "prodotto_tecnologia": {
    "descrizione": "descrizione del prodotto/servizio",
    "caratteristiche_chiave": ["feature 1", "feature 2", "..."],
    "stack_tecnologico": "tecnologie utilizzate, se menzionate (o 'Non specificato')",
    "stadio_di_sviluppo": "es. idea, MVP, beta, prodotto lanciato, revenue positivo"
  },
  "team": {
    "fondatori": [
      {"nome": "...", "ruolo": "...", "background": "sintesi del background rilevante"}
    ],
    "note_sul_team": "valutazione complessiva del team: punti di forza e gap evidenti"
  },
  "mercato": {
    "settore": "settore/industria principale",
    "sottosettore": "nicchia o verticale specifico",
    "dimensione_mercato": "TAM/SAM/SOM se dichiarati, altrimenti stima qualitativa",
    "crescita": "tasso di crescita del mercato se menzionato",
    "posizione_nella_value_chain": "dove si inserisce l'azienda nella catena del valore del settore (es. upstream/downstream, B2B/B2C, piattaforma/tool/servizio, enabler/operatore, ecc.)",
    "dinamiche_di_mercato": "trend rilevanti o driver di crescita menzionati"
  },
  "arena_competitiva": {
    "player_globali": [
      {"nome": "...", "descrizione": "in cosa competono e come si differenziano"}
    ],
    "player_europei": [
      {"nome": "...", "descrizione": "in cosa competono e come si differenziano"}
    ],
    "vantaggio_competitivo_dichiarato": "il differenziatore principale che l'azienda dichiara",
    "analisi_competitiva": "valutazione critica del posizionamento competitivo"
  },
  "domande_di_approfondimento": [
    "Domanda 1 specifica e rilevante?",
    "Domanda 2 ...",
    "Domanda 3 ...",
    "Domanda 4 ...",
    "Domanda 5 ..."
  ],
  "punti_di_attenzione": [
    {
      "area": "es. Team / Mercato / Prodotto / Financials / Legale / ecc.",
      "descrizione": "descrizione del punto critico o rischio da investigare"
    }
  ],
  "sintesi_investimento": "paragrafo di sintesi: perché questo deal potrebbe essere interessante (o meno), i 2-3 elementi più rilevanti"
}"""

# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{company_name} — Pitch Analysis</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 14px;
    line-height: 1.6;
    color: #1a1a2e;
    background: #f5f5f7;
    padding: 32px 16px;
  }}
  .container {{
    max-width: 900px;
    margin: 0 auto;
  }}
  .header {{
    background: #1a1a2e;
    color: white;
    border-radius: 12px;
    padding: 32px;
    margin-bottom: 24px;
  }}
  .header h1 {{
    font-size: 28px;
    font-weight: 700;
    letter-spacing: -0.5px;
    margin-bottom: 6px;
  }}
  .header .tagline {{
    color: #a0aec0;
    font-size: 15px;
    font-style: italic;
    margin-bottom: 12px;
  }}
  .header .meta {{
    font-size: 12px;
    color: #718096;
  }}
  .section {{
    background: white;
    border-radius: 12px;
    padding: 24px;
    margin-bottom: 16px;
    border: 1px solid #e2e8f0;
  }}
  .section h2 {{
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #718096;
    margin-bottom: 16px;
    padding-bottom: 10px;
    border-bottom: 1px solid #e2e8f0;
  }}
  .grid-2 {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }}
  @media (max-width: 600px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
  .field-label {{
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #a0aec0;
    margin-bottom: 4px;
  }}
  .field-value {{
    font-size: 14px;
    color: #2d3748;
    line-height: 1.6;
  }}
  .field-block {{ margin-bottom: 16px; }}
  .field-block:last-child {{ margin-bottom: 0; }}
  .badge {{
    display: inline-block;
    background: #edf2ff;
    color: #3b82f6;
    border-radius: 6px;
    padding: 2px 10px;
    font-size: 12px;
    font-weight: 600;
    margin: 2px;
  }}
  .badge.green {{ background: #f0fff4; color: #38a169; }}
  .badge.orange {{ background: #fffaf0; color: #dd6b20; }}
  .competitor-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }}
  @media (max-width: 600px) {{ .competitor-grid {{ grid-template-columns: 1fr; }} }}
  .competitor-card {{
    background: #f7fafc;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 12px;
  }}
  .competitor-card .c-name {{
    font-weight: 700;
    font-size: 14px;
    margin-bottom: 4px;
  }}
  .competitor-card .c-label {{
    font-size: 11px;
    color: #a0aec0;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 8px;
  }}
  .competitor-card .c-desc {{
    font-size: 13px;
    color: #4a5568;
    line-height: 1.5;
  }}
  .question-list {{
    list-style: none;
    counter-reset: q;
  }}
  .question-list li {{
    counter-increment: q;
    display: flex;
    gap: 10px;
    padding: 10px 0;
    border-bottom: 1px solid #f0f0f0;
  }}
  .question-list li:last-child {{ border-bottom: none; }}
  .question-list li::before {{
    content: counter(q);
    background: #1a1a2e;
    color: white;
    font-size: 11px;
    font-weight: 700;
    width: 20px;
    height: 20px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    margin-top: 2px;
  }}
  .flag-card {{
    background: #fffbf0;
    border: 1px solid #fbd38d;
    border-radius: 8px;
    padding: 12px 14px;
    margin-bottom: 10px;
  }}
  .flag-card:last-child {{ margin-bottom: 0; }}
  .flag-card .flag-area {{
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: #dd6b20;
    margin-bottom: 4px;
  }}
  .flag-card .flag-desc {{
    font-size: 13px;
    color: #4a5568;
    line-height: 1.5;
  }}
  .synthesis {{
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    border-radius: 12px;
    padding: 24px;
    margin-bottom: 16px;
  }}
  .synthesis h2 {{
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: rgba(255,255,255,0.7);
    margin-bottom: 12px;
  }}
  .synthesis p {{
    font-size: 15px;
    line-height: 1.7;
    color: rgba(255,255,255,0.95);
  }}
  .team-card {{
    background: #f7fafc;
    border-radius: 8px;
    padding: 12px;
    margin-bottom: 8px;
  }}
  .team-card:last-child {{ margin-bottom: 0; }}
  .team-card .t-name {{
    font-weight: 700;
    font-size: 14px;
  }}
  .team-card .t-role {{
    font-size: 12px;
    color: #718096;
    margin-bottom: 4px;
  }}
  .team-card .t-bg {{
    font-size: 13px;
    color: #4a5568;
  }}
  .features-list {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }}
  .footer {{
    text-align: center;
    color: #a0aec0;
    font-size: 12px;
    margin-top: 24px;
    padding: 16px;
  }}
</style>
</head>
<body>
<div class="container">

  <!-- Header -->
  <div class="header">
    <h1>{company_name}</h1>
    {tagline_html}
    <div class="meta">Analisi generata il {date} · Claude Opus 4.6</div>
  </div>

  <!-- Sintesi investimento -->
  <div class="synthesis">
    <h2>Sintesi</h2>
    <p>{sintesi_investimento}</p>
  </div>

  <!-- Business -->
  <div class="section">
    <h2>Business</h2>
    <div class="grid-2">
      <div>
        <div class="field-block">
          <div class="field-label">Problema</div>
          <div class="field-value">{problema}</div>
        </div>
        <div class="field-block">
          <div class="field-label">Soluzione</div>
          <div class="field-value">{soluzione}</div>
        </div>
      </div>
      <div>
        <div class="field-block">
          <div class="field-label">Modello di business</div>
          <div class="field-value">{modello_di_business}</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Prodotto & Tech -->
  <div class="section">
    <h2>Prodotto & Tecnologia</h2>
    <div class="field-block">
      <div class="field-label">Descrizione</div>
      <div class="field-value">{descrizione_prodotto}</div>
    </div>
    <div class="field-block">
      <div class="field-label">Caratteristiche chiave</div>
      <div class="features-list">{features_html}</div>
    </div>
    <div class="grid-2">
      <div class="field-block">
        <div class="field-label">Stack tecnologico</div>
        <div class="field-value">{stack_tecnologico}</div>
      </div>
      <div class="field-block">
        <div class="field-label">Stadio di sviluppo</div>
        <div class="field-value"><span class="badge orange">{stadio_di_sviluppo}</span></div>
      </div>
    </div>
  </div>

  <!-- Team -->
  <div class="section">
    <h2>Team</h2>
    {team_html}
    <div class="field-block" style="margin-top:16px">
      <div class="field-label">Valutazione del team</div>
      <div class="field-value">{note_sul_team}</div>
    </div>
  </div>

  <!-- Mercato -->
  <div class="section">
    <h2>Mercato</h2>
    <div class="grid-2">
      <div>
        <div class="field-block">
          <div class="field-label">Settore</div>
          <div class="field-value"><span class="badge">{settore}</span> <span class="badge">{sottosettore}</span></div>
        </div>
        <div class="field-block">
          <div class="field-label">Dimensione mercato</div>
          <div class="field-value">{dimensione_mercato}</div>
        </div>
        <div class="field-block">
          <div class="field-label">Crescita</div>
          <div class="field-value">{crescita}</div>
        </div>
      </div>
      <div>
        <div class="field-block">
          <div class="field-label">Posizione nella value chain</div>
          <div class="field-value">{posizione_nella_value_chain}</div>
        </div>
        <div class="field-block">
          <div class="field-label">Dinamiche di mercato</div>
          <div class="field-value">{dinamiche_di_mercato}</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Competizione -->
  <div class="section">
    <h2>Arena Competitiva</h2>
    <div class="field-block">
      <div class="field-label">Differenziatore dichiarato</div>
      <div class="field-value">{vantaggio_competitivo_dichiarato}</div>
    </div>
    <div class="field-block">
      <div class="field-label">Analisi competitiva</div>
      <div class="field-value">{analisi_competitiva}</div>
    </div>
    {competitors_section}
  </div>

  <!-- Domande -->
  <div class="section">
    <h2>Domande di Approfondimento</h2>
    <ol class="question-list">
      {questions_html}
    </ol>
  </div>

  <!-- Red flags -->
  <div class="section">
    <h2>Punti di Attenzione</h2>
    {flags_html}
  </div>

  <div class="footer">
    Generato da pitch-analyzer · {pdf_filename}
  </div>

</div>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_pdf_base64(pdf_path: Path) -> str:
    with open(pdf_path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


def extract_json(text: str) -> dict:
    """Estrae il JSON dalla risposta, gestendo markdown code blocks."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def render_html(data: dict, pdf_filename: str) -> str:
    company_name = data.get("company_name", "Startup")
    tagline = data.get("tagline", "")
    tagline_html = f'<div class="tagline">"{tagline}"</div>' if tagline else ""

    biz = data.get("business_description", {})
    prod = data.get("prodotto_tecnologia", {})
    team_data = data.get("team", {})
    market = data.get("mercato", {})
    comp = data.get("arena_competitiva", {})

    features = prod.get("caratteristiche_chiave", [])
    features_html = "".join(f'<span class="badge green">{f}</span>' for f in features)

    fondatori = team_data.get("fondatori", [])
    team_html = ""
    for p in fondatori:
        team_html += f"""<div class="team-card">
          <div class="t-name">{p.get('nome', '')}</div>
          <div class="t-role">{p.get('ruolo', '')}</div>
          <div class="t-bg">{p.get('background', '')}</div>
        </div>"""

    def render_competitors(players: list, label: str) -> str:
        if not players:
            return ""
        cards = ""
        for c in players:
            cards += f"""<div class="competitor-card">
              <div class="c-label">{label}</div>
              <div class="c-name">{c.get('nome', '')}</div>
              <div class="c-desc">{c.get('descrizione', '')}</div>
            </div>"""
        return cards

    global_cards = render_competitors(comp.get("player_globali", []), "Globale")
    eu_cards = render_competitors(comp.get("player_europei", []), "Europeo")
    all_cards = global_cards + eu_cards
    competitors_section = ""
    if all_cards:
        competitors_section = f'<div class="competitor-grid">{all_cards}</div>'

    questions = data.get("domande_di_approfondimento", [])
    questions_html = "".join(f"<li>{q}</li>" for q in questions)

    flags = data.get("punti_di_attenzione", [])
    flags_html = ""
    for f in flags:
        flags_html += f"""<div class="flag-card">
          <div class="flag-area">{f.get('area', '')}</div>
          <div class="flag-desc">{f.get('descrizione', '')}</div>
        </div>"""
    if not flags_html:
        flags_html = '<p style="color:#a0aec0">Nessun punto critico identificato.</p>'

    return HTML_TEMPLATE.format(
        company_name=company_name,
        tagline_html=tagline_html,
        date=datetime.now().strftime("%d %B %Y"),
        sintesi_investimento=data.get("sintesi_investimento", ""),
        problema=biz.get("problema", ""),
        soluzione=biz.get("soluzione", ""),
        modello_di_business=biz.get("modello_di_business", ""),
        descrizione_prodotto=prod.get("descrizione", ""),
        features_html=features_html,
        stack_tecnologico=prod.get("stack_tecnologico", "Non specificato"),
        stadio_di_sviluppo=prod.get("stadio_di_sviluppo", ""),
        team_html=team_html,
        note_sul_team=team_data.get("note_sul_team", ""),
        settore=market.get("settore", ""),
        sottosettore=market.get("sottosettore", ""),
        dimensione_mercato=market.get("dimensione_mercato", ""),
        crescita=market.get("crescita", ""),
        posizione_nella_value_chain=market.get("posizione_nella_value_chain", ""),
        dinamiche_di_mercato=market.get("dinamiche_di_mercato", ""),
        vantaggio_competitivo_dichiarato=comp.get("vantaggio_competitivo_dichiarato", ""),
        analisi_competitiva=comp.get("analisi_competitiva", ""),
        competitors_section=competitors_section,
        questions_html=questions_html,
        flags_html=flags_html,
        pdf_filename=pdf_filename,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Analizza un pitch deck PDF con Claude."
    )
    parser.add_argument("pdf", help="Percorso al file PDF del pitch deck")
    parser.add_argument(
        "--output", "-o", help="Percorso output HTML (default: <nome_pdf>_analysis.html)"
    )
    parser.add_argument(
        "--no-open", action="store_true", help="Non aprire il browser automaticamente"
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        print(f"Errore: file non trovato: {pdf_path}", file=sys.stderr)
        sys.exit(1)
    if pdf_path.suffix.lower() != ".pdf":
        print("Attenzione: il file non ha estensione .pdf", file=sys.stderr)

    output_path = Path(args.output) if args.output else pdf_path.with_suffix("_analysis.html")

    print(f"Carico PDF: {pdf_path.name} ({pdf_path.stat().st_size / 1024:.0f} KB)")
    pdf_b64 = load_pdf_base64(pdf_path)

    client = anthropic.Anthropic()

    print("Invio a Claude Opus 4.6 per l'analisi...")

    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=4096,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {"type": "text", "text": ANALYSIS_PROMPT},
                ],
            }
        ],
    ) as stream:
        for event in stream:
            if (
                hasattr(event, "type")
                and event.type == "content_block_start"
                and hasattr(event, "content_block")
                and event.content_block.type == "thinking"
            ):
                print("  → Analisi in corso (thinking)...", end="\r")

        final = stream.get_final_message()

    text_content = ""
    for block in final.content:
        if block.type == "text":
            text_content = block.text
            break

    if not text_content:
        print("Errore: nessuna risposta testuale ricevuta.", file=sys.stderr)
        sys.exit(1)

    print("  → Analisi completata.           ")

    try:
        data = extract_json(text_content)
    except json.JSONDecodeError as e:
        print(f"Errore nel parsing JSON: {e}", file=sys.stderr)
        print("Risposta grezza:", file=sys.stderr)
        print(text_content[:500], file=sys.stderr)
        sys.exit(1)

    html = render_html(data, pdf_path.name)
    output_path.write_text(html, encoding="utf-8")
    print(f"Report salvato: {output_path}")

    if not args.no_open:
        webbrowser.open(output_path.as_uri())

    print("\n" + "─" * 60)
    company = data.get("company_name", "N/A")
    settore = data.get("mercato", {}).get("settore", "N/A")
    stadio = data.get("prodotto_tecnologia", {}).get("stadio_di_sviluppo", "N/A")
    print(f"  Azienda : {company}")
    print(f"  Settore : {settore}")
    print(f"  Stadio  : {stadio}")
    print(f"  Report  : {output_path}")
    print("─" * 60)


if __name__ == "__main__":
    main()

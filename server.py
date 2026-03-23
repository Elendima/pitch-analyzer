#!/usr/bin/env python3
"""
Pitch Analyzer — Server locale
Avvia con: python3 server.py
Poi apri: http://localhost:5000
"""

import json
import os
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
from flask import Flask, jsonify, render_template_string, request, send_from_directory
from openai import OpenAI

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
client = OpenAI()

# ---------------------------------------------------------------------------
# Pagina indice
# ---------------------------------------------------------------------------

INDEX_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pitch Analyzer</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f5f5f7; padding: 40px 16px; color: #1a1a2e; }
  .container { max-width: 700px; margin: 0 auto; }
  h1 { font-size: 24px; font-weight: 700; margin-bottom: 8px; }
  .sub { color: #718096; margin-bottom: 32px; font-size: 14px; }
  .card { background: white; border-radius: 12px; border: 1px solid #e2e8f0;
          padding: 18px 22px; margin-bottom: 12px; display: flex;
          align-items: center; justify-content: space-between; }
  .card-name { font-weight: 700; font-size: 15px; }
  .card-date { font-size: 12px; color: #a0aec0; margin-top: 2px; }
  .btn { background: #1a1a2e; color: white; border-radius: 8px;
         padding: 8px 16px; font-size: 13px; font-weight: 600;
         text-decoration: none; white-space: nowrap; }
  .empty { color: #a0aec0; text-align: center; padding: 40px; }
</style>
</head>
<body>
<div class="container">
  <h1>Pitch Analyzer</h1>
  <p class="sub">Le tue analisi recenti</p>
  {% if analyses %}
    {% for a in analyses %}
    <div class="card">
      <div>
        <div class="card-name">{{ a.name }}</div>
        <div class="card-date">{{ a.date }}</div>
      </div>
      <a class="btn" href="/analisi/{{ a.file }}">Apri →</a>
    </div>
    {% endfor %}
  {% else %}
    <div class="empty">Nessuna analisi ancora.<br>Lancia <code>python3 analyze_pitch.py deck.pdf</code> per iniziare.</div>
  {% endif %}
</div>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Prompt confronto
# ---------------------------------------------------------------------------

COMPARE_PROMPT = """Sei un analista VC. Confronta queste due società e restituisci SOLO un JSON.
Tutto in italiano.

SOCIETÀ A (dal pitch deck analizzato):
{analisi_a}

SOCIETÀ B (da ricerca web):
{contesto_b}

Restituisci questo JSON esatto:
{{
  "societa_b": {{
    "nome": "...",
    "descrizione": "Cosa fa in 2 righe",
    "modello": "Come genera ricavi",
    "mercato": "In quale mercato opera",
    "stadio": "Stadio di sviluppo stimato",
    "funding": "Funding noto se disponibile, altrimenti 'Non disponibile'"
  }},
  "confronto": {{
    "modello_di_business": {{
      "a": "Punto di forza/debolezza di A",
      "b": "Punto di forza/debolezza di B",
      "vantaggio": "A / B / Pari"
    }},
    "posizionamento_mercato": {{
      "a": "Come si posiziona A",
      "b": "Come si posiziona B",
      "vantaggio": "A / B / Pari"
    }},
    "tecnologia_e_prodotto": {{
      "a": "Punto chiave di A",
      "b": "Punto chiave di B",
      "vantaggio": "A / B / Pari"
    }},
    "traction_e_momentum": {{
      "a": "Trazione nota di A",
      "b": "Trazione nota di B",
      "vantaggio": "A / B / Pari"
    }},
    "team": {{
      "a": "Punto chiave del team A",
      "b": "Punto chiave del team B",
      "vantaggio": "A / B / Pari"
    }}
  }},
  "sintesi": "2-3 frasi: chi è più avanti, perché, e quale delle due ha il vantaggio competitivo più difendibile."
}}"""

# ---------------------------------------------------------------------------
# Web research
# ---------------------------------------------------------------------------

def scrape_url(url, max_chars=2000):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        return soup.get_text(" ", strip=True)[:max_chars]
    except Exception:
        return ""

def web_research_company(nome):
    results = []
    try:
        with DDGS() as ddgs:
            for hit in ddgs.text(f"{nome} startup funding", max_results=5):
                results.append(f"[{hit.get('title','')}] {hit.get('body','')}")
            # Prova a trovare il sito ufficiale
            for hit in ddgs.text(f"{nome} official website", max_results=2):
                url = hit.get("href", "")
                if url and nome.lower().replace(" ", "") in url.lower():
                    content = scrape_url(url)
                    if content:
                        results.insert(0, f"[SITO UFFICIALE] {content}")
                    break
    except Exception as e:
        results.append(f"Ricerca limitata: {e}")
    return "\n\n".join(results) if results else "Nessuna informazione trovata."

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    files = sorted(OUTPUT_DIR.glob("*_analisi.html"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    analyses = []
    for f in files:
        import datetime
        mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime)
        analyses.append({
            "name": f.stem.replace("_analisi", ""),
            "file": f.name,
            "date": mtime.strftime("%d %b %Y, %H:%M"),
        })
    return render_template_string(INDEX_HTML, analyses=analyses)


@app.route("/analisi/<path:filename>")
def serve_analysis(filename):
    return send_from_directory(OUTPUT_DIR, filename)


@app.route("/compare", methods=["POST"])
def compare():
    payload = request.json
    nome_b = payload.get("company_name", "").strip()
    analisi_a = payload.get("analysis", {})

    if not nome_b:
        return jsonify({"error": "Nome società mancante"}), 400

    # Ricerca web su società B
    contesto_b = web_research_company(nome_b)

    # Sintesi analisi A per il prompt
    sintesi_a = json.dumps({
        "nome": analisi_a.get("nome_azienda", ""),
        "business": analisi_a.get("business", {}),
        "mercato": analisi_a.get("mercato", {}),
        "prodotto": analisi_a.get("prodotto_tecnologia", {}),
        "team": analisi_a.get("team", {}),
    }, ensure_ascii=False, indent=2)

    prompt = COMPARE_PROMPT.format(analisi_a=sintesi_a, contesto_b=contesto_b)

    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Sei un analista VC. Rispondi solo in italiano con JSON valido."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
        max_tokens=2000,
    )

    result = json.loads(resp.choices[0].message.content)
    return jsonify(result)


if __name__ == "__main__":
    print("=" * 50)
    print("  Pitch Analyzer Server")
    print("  Apri: http://localhost:5000")
    print("  Premi Ctrl+C per fermare")
    print("=" * 50)
    app.run(port=5000, debug=False)

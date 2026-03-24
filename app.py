#!/usr/bin/env python3
"""
Pitch Analyzer — Web UI con drag-and-drop
Avvia con: python3 app.py
Poi apri:  http://localhost:5000
"""

import json
import os
import tempfile
import threading
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request, send_from_directory

# Import del core analyzer
from analyze_pitch import (
    extract_text_from_pdf,
    phase1_extract,
    phase2_analyze,
    render_html,
    web_research,
    OpenAI,
)

app = Flask(__name__)
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# UI principale
# ---------------------------------------------------------------------------

UI = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pitch Analyzer</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
  background: #fcfbf8; color: #1c1917; min-height: 100vh;
  display: flex; flex-direction: column;
}
.wrap { max-width: 680px; margin: 0 auto; padding: 60px 16px; flex: 1; }
.logo { font-size: 13px; font-weight: 700; letter-spacing: .08em;
  text-transform: uppercase; color: #a8a29e; margin-bottom: 48px; }
h1 { font-size: 36px; font-weight: 700; letter-spacing: -.8px;
  line-height: 1.2; margin-bottom: 10px; }
.sub { font-size: 16px; color: #57534e; margin-bottom: 48px; }

/* Drop zone */
#dropzone {
  border: 2px dashed #e8e3da; border-radius: 16px;
  padding: 56px 32px; text-align: center; cursor: pointer;
  transition: border-color .2s, background .2s;
  background: #fff; position: relative;
}
#dropzone.drag { border-color: #18181b; background: #f5f0e8; }
#dropzone.loading { border-color: #18181b; background: #fff; cursor: default; }
#dz-icon { font-size: 40px; margin-bottom: 16px; }
#dz-title { font-size: 16px; font-weight: 600; color: #1c1917; margin-bottom: 6px; }
#dz-sub { font-size: 14px; color: #a8a29e; }
#file-input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
#file-input.hide { display: none; }

/* Spinner */
.spinner {
  width: 32px; height: 32px; border: 3px solid #e8e3da;
  border-top-color: #18181b; border-radius: 50%;
  animation: spin .8s linear infinite; margin: 0 auto 16px;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* Status log */
#log { margin-top: 20px; font-size: 13px; color: #57534e;
  background: #fff; border: 1px solid #e8e3da; border-radius: 12px;
  padding: 16px; display: none; max-height: 140px; overflow-y: auto; }
#log p { padding: 3px 0; }
#log p:last-child { color: #18181b; font-weight: 600; }

/* Risultati recenti */
.recent { margin-top: 48px; }
.recent-title { font-size: 11px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .08em; color: #a8a29e; margin-bottom: 14px; }
.recent-card {
  background: #fff; border: 1px solid #e8e3da; border-radius: 12px;
  padding: 14px 18px; margin-bottom: 10px; display: flex;
  align-items: center; justify-content: space-between; gap: 12px;
}
.rc-name { font-weight: 600; font-size: 14px; }
.rc-date { font-size: 12px; color: #a8a29e; margin-top: 2px; }
.rc-btn { background: #18181b; color: #fff; border-radius: 8px;
  padding: 6px 14px; font-size: 12px; font-weight: 600;
  text-decoration: none; white-space: nowrap; }
.empty { color: #a8a29e; font-size: 14px; text-align: center; padding: 24px 0; }
</style>
</head>
<body>
<div class="wrap">
  <div class="logo">Pitch Analyzer</div>
  <h1>Analizza il tuo<br>pitch deck</h1>
  <p class="sub">Trascina un PDF qui sotto per avviare l'analisi.</p>

  <div id="dropzone">
    <input type="file" id="file-input" accept=".pdf">
    <div id="dz-icon">📄</div>
    <div id="dz-title">Trascina il PDF qui</div>
    <div id="dz-sub">o clicca per selezionarlo</div>
  </div>

  <div id="log"></div>

  <div class="recent" id="recent-section">
    <div class="recent-title">Analisi recenti</div>
    <div id="recent-list"><div class="empty">Nessuna analisi ancora.</div></div>
  </div>
</div>

<script>
const dz    = document.getElementById('dropzone');
const input = document.getElementById('file-input');
const log   = document.getElementById('log');

function setDzState(state) {
  dz.className = state;
  const icon  = document.getElementById('dz-icon');
  const title = document.getElementById('dz-title');
  const sub   = document.getElementById('dz-sub');
  if (state === 'loading') {
    icon.innerHTML  = '<div class="spinner"></div>';
    icon.style.fontSize = '';
    title.textContent = 'Analisi in corso…';
    sub.textContent   = 'Può richiedere 1-2 minuti';
    input.className   = 'hide';
  } else {
    icon.innerHTML    = '📄';
    icon.style.fontSize = '40px';
    title.textContent = 'Trascina il PDF qui';
    sub.textContent   = 'o clicca per selezionarlo';
    input.className   = '';
  }
}

function addLog(msg) {
  log.style.display = 'block';
  const p = document.createElement('p');
  p.textContent = '→ ' + msg;
  log.appendChild(p);
  log.scrollTop = log.scrollHeight;
}

function clearLog() { log.innerHTML = ''; log.style.display = 'none'; }

async function upload(file) {
  if (!file || file.type !== 'application/pdf') {
    alert('Seleziona un file PDF.'); return;
  }
  clearLog();
  setDzState('loading');
  addLog('Carico il PDF: ' + file.name);

  const fd = new FormData();
  fd.append('pdf', file);

  try {
    const resp = await fetch('/analyze', { method: 'POST', body: fd });
    const data = await resp.json();
    if (data.error) { addLog('Errore: ' + data.error); setDzState(''); return; }
    addLog('Analisi completata!');
    loadRecent();
    window.open('/result/' + data.filename, '_blank');
  } catch(e) {
    addLog('Errore di rete: ' + e.message);
  } finally {
    setDzState('');
  }
}

dz.addEventListener('dragover', e => { e.preventDefault(); if (!dz.classList.contains('loading')) dz.classList.add('drag'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('drag');
  const f = e.dataTransfer.files[0]; if (f) upload(f);
});
input.addEventListener('change', e => { const f = e.target.files[0]; if (f) upload(f); });

async function loadRecent() {
  const r = await fetch('/recent');
  const items = await r.json();
  const el = document.getElementById('recent-list');
  if (!items.length) { el.innerHTML = '<div class="empty">Nessuna analisi ancora.</div>'; return; }
  el.innerHTML = items.map(i => `
    <div class="recent-card">
      <div>
        <div class="rc-name">${i.name}</div>
        <div class="rc-date">${i.date}</div>
      </div>
      <a class="rc-btn" href="/result/${i.file}" target="_blank">Apri →</a>
    </div>`).join('');
}

loadRecent();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(UI)


@app.route("/recent")
def recent():
    files = sorted(OUTPUT_DIR.glob("*_analisi.html"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    import datetime
    result = []
    for f in files[:10]:
        mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime)
        result.append({
            "name": f.stem.replace("_analisi", ""),
            "file": f.name,
            "date": mtime.strftime("%d %b %Y, %H:%M"),
        })
    return jsonify(result)


@app.route("/result/<path:filename>")
def result(filename):
    return send_from_directory(OUTPUT_DIR, filename)


@app.route("/analyze", methods=["POST"])
def analyze():
    if "pdf" not in request.files:
        return jsonify({"error": "Nessun file ricevuto"}), 400

    pdf_file = request.files["pdf"]
    if not pdf_file.filename.endswith(".pdf"):
        return jsonify({"error": "Il file deve essere un PDF"}), 400

    # Salva temporaneamente
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        pdf_file.save(tmp.name)
        tmp_path = Path(tmp.name)

    try:
        client = OpenAI()
        pdf_text = extract_text_from_pdf(tmp_path, client)
        if not pdf_text.strip():
            return jsonify({"error": "Nessun testo estratto dal PDF."}), 400
        meta = phase1_extract(client, pdf_text)
        nome = meta.get("nome_azienda", "Startup")
        sito = meta.get("sito_web") or ""
        settore = meta.get("settore", "")

        web_ctx = web_research(nome, sito, settore)
        web_used = bool(web_ctx and "Nessuna" not in web_ctx)

        data = phase2_analyze(client, nome, pdf_text, web_ctx)

        html = render_html(data, pdf_file.filename, web_used)
        out_name = Path(pdf_file.filename).stem + "_analisi.html"
        out_path = OUTPUT_DIR / out_name
        out_path.write_text(html, encoding="utf-8")

        return jsonify({"filename": out_name, "company": nome})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("⚠️  OPENAI_API_KEY non impostata. Esporta la variabile prima di avviare.")
        print("   export OPENAI_API_KEY=sk-proj-...")
        exit(1)

    print("=" * 50)
    print("  Pitch Analyzer")
    print("  Apri: http://127.0.0.1:5000")
    print("  Premi Ctrl+C per fermare")
    print("=" * 50)

    threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="127.0.0.1", port=5000, debug=False)

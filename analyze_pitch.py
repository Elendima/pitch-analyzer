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
# HTML template  — React + Lovable design
# Note: uses str.replace() not .format(), so no {{ }} escaping needed.
# Placeholders: __ANALYSIS_JSON__  __WEB_USED__  __PDF_FILENAME__
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Analisi Pitch Deck</title>
<style>* { box-sizing: border-box; margin: 0; padding: 0; } html { scroll-behavior: smooth; } body { background: #fcfbf8; }</style>
</head>
<body>
<div id="root"><div style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:system-ui;color:#a8a29e;font-size:14px">Caricamento…</div></div>
<script>
window.__DATA__ = __ANALYSIS_JSON__;
window.__WEB__  = __WEB_USED__;
window.__FILE__ = __PDF_FILENAME__;
</script>
<script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
<script type="text/babel" data-presets="react">
const { useState } = React;
const D    = window.__DATA__ || {};
const web  = window.__WEB__;
const file = window.__FILE__ || '';

const C = {
  bg:'#fcfbf8', surface:'#fff', border:'#e8e3da',
  t1:'#1c1917', t2:'#57534e', t3:'#a8a29e', dark:'#18181b',
  red:'#dc2626',    redBg:'#fef2f2',    redBr:'#fecaca',
  orange:'#c2410c', orangeBg:'#fff7ed', orangeBr:'#fed7aa',
  green:'#15803d',  greenBg:'#f0fdf4',
  blue:'#1d4ed8',   blueBg:'#eff6ff',
};

const Card = ({children, id}) => (
  <div id={id} style={{background:C.surface, border:`1px solid ${C.border}`, borderRadius:16,
    padding:'26px 28px', marginBottom:16, boxShadow:'0 1px 4px rgba(0,0,0,.05)'}}>
    {children}
  </div>
);

const CardTitle = ({children}) => (
  <div style={{fontSize:11,fontWeight:700,letterSpacing:'.1em',textTransform:'uppercase',
    color:C.t3,paddingBottom:14,borderBottom:`1px solid ${C.border}`,marginBottom:20}}>
    {children}
  </div>
);

const Field = ({label, children, mt}) => (
  <div style={{marginBottom:20, marginTop:mt||0}}>
    <div style={{fontSize:10,fontWeight:700,letterSpacing:'.07em',textTransform:'uppercase',
      color:C.t3,marginBottom:6}}>{label}</div>
    <div style={{fontSize:14,color:C.t2,lineHeight:1.7}}>{children}</div>
  </div>
);

const Tag = ({children, v='gray'}) => {
  const m = {
    blue:[C.blueBg,C.blue], green:[C.greenBg,C.green],
    orange:[C.orangeBg,C.orange], red:[C.redBg,C.red], gray:['#f4f4f5','#52525b']
  };
  const [bg,color] = m[v]||m.gray;
  return <span style={{display:'inline-block',background:bg,color,borderRadius:6,
    padding:'2px 10px',fontSize:12,fontWeight:600,margin:2}}>{children}</span>;
};

const Pill = ({children, teal}) => (
  <span style={{display:'inline-flex',alignItems:'center',
    background:teal?'#f0fdfa':C.surface, border:`1px solid ${teal?'#99f6e4':C.border}`,
    color:teal?'#0f766e':C.t2, borderRadius:99, padding:'3px 12px',
    fontSize:12, fontWeight:500, marginRight:6}}>{children}</span>
);

function NavBar() {
  const sections = [
    ['business','Business'],['prodotto','Prodotto'],['team','Team'],
    ['mercato','Mercato'],['competizione','Competizione'],
    ['domande','Domande'],['rischi','Rischi'],
  ];
  const [hov, setHov] = useState(null);
  return (
    <div style={{position:'sticky',top:0,zIndex:50,background:'rgba(252,251,248,.92)',
      backdropFilter:'blur(8px)',borderBottom:`1px solid ${C.border}`,padding:'10px 0',marginBottom:24}}>
      <div style={{maxWidth:940,margin:'0 auto',padding:'0 16px',display:'flex',gap:4,overflowX:'auto'}}>
        {sections.map(([id,label]) => (
          <a key={id} href={`#${id}`}
            onMouseEnter={()=>setHov(id)} onMouseLeave={()=>setHov(null)}
            style={{display:'inline-flex',alignItems:'center',padding:'5px 12px',borderRadius:8,
              fontSize:13,fontWeight:500,color:C.t2,textDecoration:'none',whiteSpace:'nowrap',
              background:hov===id?C.border:'transparent',transition:'background .15s'}}>
            {label}
          </a>
        ))}
      </div>
    </div>
  );
}

function Header() {
  const nome = D.nome_azienda||'';
  const tag  = D.tagline||'';
  const date = new Date().toLocaleDateString('it-IT',{day:'numeric',month:'long',year:'numeric'});
  return (
    <div style={{padding:'40px 0 28px',borderBottom:`1px solid ${C.border}`,marginBottom:28}}>
      <div style={{fontSize:11,fontWeight:700,letterSpacing:'.08em',textTransform:'uppercase',
        color:C.t3,marginBottom:10}}>Analisi Pitch Deck</div>
      <h1 style={{fontSize:38,fontWeight:700,letterSpacing:'-.8px',color:C.t1,
        lineHeight:1.15,marginBottom:8}}>{nome}</h1>
      {tag && <div style={{fontSize:16,color:C.t2,marginBottom:12,fontStyle:'italic'}}>"{tag}"</div>}
      <div style={{display:'flex',flexWrap:'wrap',gap:6,alignItems:'center',marginTop:8}}>
        <Pill>{date}</Pill>
        <Pill>GPT-4o</Pill>
        {web && <Pill teal>+ ricerca web</Pill>}
      </div>
    </div>
  );
}

function Synthesis() {
  return (
    <div style={{background:C.dark,borderRadius:16,padding:'28px 30px',marginBottom:16}}>
      <div style={{fontSize:10,fontWeight:700,letterSpacing:'.1em',textTransform:'uppercase',
        color:'rgba(255,255,255,.4)',marginBottom:10}}>Sintesi del Partner</div>
      <p style={{fontSize:15,lineHeight:1.8,color:'rgba(255,255,255,.9)'}}>{D.sintesi}</p>
    </div>
  );
}

function BusinessSection() {
  const b = D.business||{};
  return (
    <Card id="business">
      <CardTitle>Business</CardTitle>
      <Field label="Problema">{b.problema}</Field>
      <Field label="Soluzione">{b.soluzione}</Field>
      <Field label="Modello di business" mt={0}>{b.modello_di_business}</Field>
    </Card>
  );
}

function ProductSection() {
  const p = D.prodotto_tecnologia||{};
  const feats = p.caratteristiche_chiave||[];
  return (
    <Card id="prodotto">
      <CardTitle>Prodotto e Tecnologia</CardTitle>
      <Field label="Descrizione">{p.descrizione}</Field>
      <Field label="Caratteristiche chiave">
        <div style={{display:'flex',flexWrap:'wrap',gap:4,marginTop:4}}>
          {feats.map((f,i)=><Tag key={i} v="green">{f}</Tag>)}
        </div>
      </Field>
      <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:20}}>
        <Field label="Stack tecnologico">{p.stack_tecnologico}</Field>
        <Field label="Stadio"><Tag v="orange">{p.stadio_di_sviluppo}</Tag></Field>
      </div>
      <Field label="Differenziatore tecnologico" mt={4}>{p.differenziatore_tecnologico}</Field>
    </Card>
  );
}

function TeamSection() {
  const t = D.team||{};
  const founders = t.fondatori||[];
  return (
    <Card id="team">
      <CardTitle>Team</CardTitle>
      {founders.map((f,i)=>(
        <div key={i} style={{background:C.bg,border:`1px solid ${C.border}`,
          borderRadius:10,padding:16,marginBottom:10}}>
          <div style={{fontWeight:700,fontSize:14,marginBottom:2}}>{f.nome}</div>
          <div style={{fontSize:12,color:C.t3,marginBottom:8}}>{f.ruolo}</div>
          <div style={{fontSize:13,color:C.t2,lineHeight:1.6}}>{f.background}</div>
        </div>
      ))}
      <Field label="Valutazione critica" mt={16}>{t.valutazione_team}</Field>
    </Card>
  );
}

function MarketSection() {
  const m = D.mercato||{};
  return (
    <Card id="mercato">
      <CardTitle>Mercato</CardTitle>
      <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:24}}>
        <div>
          <Field label="Settore">
            <Tag v="blue">{m.settore}</Tag>
            <Tag v="gray">{m.sottosettore}</Tag>
          </Field>
          <Field label="Dimensione mercato">{m.dimensione_mercato}</Field>
          <Field label="Tasso di crescita">{m.tasso_di_crescita}</Field>
          <Field label="Driver di mercato">{m.driver_di_mercato}</Field>
        </div>
        <div>
          <Field label="Struttura catena del valore">{m.struttura_della_catena_del_valore}</Field>
          <Field label="Posizionamento nella catena">{m.posizionamento_nella_catena}</Field>
          <Field label="Dipendenze strategiche">{m.dipendenze_strategiche}</Field>
        </div>
      </div>
    </Card>
  );
}

function CompetitionSection() {
  const c = D.competizione||{};
  const all = [
    ...(c.player_globali||[]).map(p=>({...p,tipo:'Globale'})),
    ...(c.player_europei||[]).map(p=>({...p,tipo:'Europeo'})),
  ];
  return (
    <Card id="competizione">
      <CardTitle>Arena Competitiva</CardTitle>
      <Field label="Vantaggio dichiarato">{c.vantaggio_competitivo_dichiarato}</Field>
      <Field label="Valutazione critica">{c.valutazione_critica_del_vantaggio}</Field>
      {all.length>0 && (
        <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:10,marginTop:8}}>
          {all.map((p,i)=>(
            <div key={i} style={{background:C.bg,border:`1px solid ${C.border}`,borderRadius:10,padding:14}}>
              <div style={{fontSize:10,fontWeight:700,textTransform:'uppercase',
                letterSpacing:'.06em',color:C.t3,marginBottom:5}}>{p.tipo}</div>
              <div style={{fontWeight:700,fontSize:14,marginBottom:6}}>{p.nome}</div>
              <div style={{fontSize:13,color:C.t2,lineHeight:1.55}}>{p.descrizione}</div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function QuestionsSection() {
  const qs = D.domande_per_il_founder||[];
  return (
    <Card id="domande">
      <CardTitle>Domande per il Founder</CardTitle>
      <ol style={{listStyle:'none'}}>
        {qs.map((q,i)=>(
          <li key={i} style={{display:'flex',gap:14,padding:'13px 0',
            borderBottom:i<qs.length-1?`1px solid ${C.border}`:'none',alignItems:'flex-start'}}>
            <span style={{background:C.dark,color:'#fff',fontSize:11,fontWeight:700,
              minWidth:22,height:22,borderRadius:'50%',display:'flex',alignItems:'center',
              justifyContent:'center',flexShrink:0,marginTop:1}}>{i+1}</span>
            <span style={{fontSize:14,color:C.t2,lineHeight:1.7}}>{q}</span>
          </li>
        ))}
      </ol>
    </Card>
  );
}

function FlagsSection() {
  const flags = D.punti_di_attenzione||[];
  return (
    <Card id="rischi">
      <CardTitle>Punti di Attenzione</CardTitle>
      {flags.length===0
        ? <p style={{color:C.t3,fontSize:14}}>Nessun punto critico identificato.</p>
        : flags.map((f,i)=>{
          const g = f.gravità||f.gravita||'Media';
          const alta = g.toLowerCase()==='alta';
          return (
            <div key={i} style={{background:alta?C.redBg:C.orangeBg,
              border:`1px solid ${alta?C.redBr:C.orangeBr}`,
              borderRadius:10,padding:'14px 16px',marginBottom:i<flags.length-1?10:0}}>
              <div style={{display:'flex',alignItems:'center',gap:8,marginBottom:7}}>
                <span style={{fontSize:10,fontWeight:700,textTransform:'uppercase',
                  letterSpacing:'.06em',color:alta?C.red:C.orange}}>{f.area}</span>
                <span style={{fontSize:10,fontWeight:700,padding:'2px 8px',borderRadius:4,
                  background:alta?C.redBr:C.orangeBr,color:alta?C.red:C.orange}}>{g}</span>
              </div>
              <div style={{fontSize:13,color:C.t2,lineHeight:1.6}}>{f.descrizione}</div>
            </div>
          );
        })
      }
    </Card>
  );
}

function App() {
  return (
    <div style={{fontFamily:'-apple-system,BlinkMacSystemFont,"Inter","Segoe UI",sans-serif',
      fontSize:14,lineHeight:'1.65',color:C.t1,background:C.bg,minHeight:'100vh'}}>
      <NavBar />
      <div style={{maxWidth:940,margin:'0 auto',padding:'0 16px 80px'}}>
        <Header />
        <Synthesis />
        <BusinessSection />
        <ProductSection />
        <TeamSection />
        <MarketSection />
        <CompetitionSection />
        <QuestionsSection />
        <FlagsSection />
        <div style={{textAlign:'center',color:C.t3,fontSize:12,marginTop:32}}>
          pitch-analyzer · {file}
        </div>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
</script>
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
    return (HTML_TEMPLATE
        .replace('__ANALYSIS_JSON__', json.dumps(data, ensure_ascii=False))
        .replace('__WEB_USED__', 'true' if web_used else 'false')
        .replace('__PDF_FILENAME__', json.dumps(pdf_filename, ensure_ascii=False)))

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

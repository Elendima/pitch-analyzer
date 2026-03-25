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

import base64
from io import BytesIO

import pdfplumber
import pypdfium2 as pdfium
import requests
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
from openai import OpenAI
from PIL import Image

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a partner at a venture capital fund with 15 years of experience,
specializing in early-stage investments in Europe and globally.
You have evaluated thousands of pitch decks and know exactly where founders lie by omission,
where the numbers don't add up, and where an opportunity is genuinely interesting.

Absolute rules:
- Reply EXCLUSIVELY in English.
- Be specific and concrete. Zero generic statements. Every claim must be anchored
  to data or facts present in the document or web sources.
- Founder names must be copied EXACTLY as they appear in the deck — never translate,
  anglicize, or alter them (e.g. "Damiano" stays "Damiano", not "Damian").
- If information is not in the document or on the web, say so explicitly.
- Do not invent anything."""

PHASE1_PROMPT = """From the pitch deck text below, extract ONLY these fields as JSON.
No other text.

{
  "nome_azienda": "...",
  "sito_web": "url if present, otherwise null",
  "settore": "main sector in English",
  "descrizione_breve": "max 2 lines on what the company does"
}

Text:
"""

PHASE2_PROMPT = """You are a VC analyst who has just read the pitch deck of {nome_azienda}
and found additional information on the web.

You have access to:
1. PITCH DECK TEXT:
{testo_deck}

2. WEB RESEARCH (use this to enrich and verify claims in the deck — especially for problem framing, market context, and competitive landscape):
{contesto_web}

Produce a deep analysis as JSON with the exact structure below.
Do not add any text outside the JSON. Everything in English.

IMPORTANT: For "problema" and "soluzione", do NOT just summarize what the deck says.
Use the web research to contextualize the problem: how large is it? What evidence exists beyond the founder's claims? What have others written about it? Make the analysis independent and well-grounded.

RULES FOR struttura_della_catena_del_valore:
Ignore the deck entirely. Build the functional stack of the END MARKET ecosystem.

FRAMEWORK: think in layers (not a linear supply chain). Identify 5-6 layers from upstream infrastructure/access down to execution. Each field must be SHORT and DENSE — no narrative, only substance.

For each layer:
- "fase": precise technical name (e.g. "Data & Access Layer", "Visibility Analytics Layer")
- "descrizione": MAX 2 sentences. What this layer controls and what structural tension characterizes it today.
- "player": 4-5 real names with role in parentheses (e.g. "OpenAI (LLM provider)", "Common Crawl (training data)")
- "margine": MAX 1 sentence. Who captures margin and with which specific lever (proprietary data / network effect / lock-in / exclusive access).
- "posizione_startup": true only if the startup operates here.

PRIORITY: completeness of layers > length of descriptions. Better 6 short layers than 2 long ones.

RULES FOR domande_per_il_founder:
Generate 7 questions starting from what this specific deck does NOT say, says vaguely, or contradicts.
Each question must be impossible to recycle on another deck: if it works the same without the company name, it is wrong and must be rewritten.
Questions must be anchored to concrete details from this deck or this specific market (cite figures, claims or founder choices).
Not answerable with yes/no. Forbidden: "What are your growth plans?", "How do you plan to differentiate?", "What are your KPIs?".
Always start from: what is missing? what doesn't add up? what is claimed but not demonstrated? what is specific to this sector that the founder hasn't explained?

{{
  "nome_azienda": "...",
  "tagline": "tagline ufficiale se presente, altrimenti null",

  "business": {{
    "problema": "Precise problem description grounded in both deck and web research. Who feels it? How intensely? What existing solutions exist and why do they fall short? Include external evidence where available.",
    "soluzione": "How it solves the problem specifically. What is the core mechanism? How does it differ from what already exists?",
    "modello_di_business": "How it generates revenue. Pricing structure if known. Revenue nature (recurring/transactional). Who pays vs who uses the product (if different)."
  }},

  "prodotto_tecnologia": {{
    "descrizione": "Functional description of the product. What does a user concretely do with this tool?",
    "caratteristiche_chiave": ["feature 1", "feature 2", "..."],
    "stack_tecnologico": "Technologies used if mentioned. If not declared, state 'Not disclosed in deck'.",
    "differenziatore_tecnologico": "Is there a real technological moat? Patents, proprietary data, proprietary algorithms? Or is this an execution play?",
    "stadio_di_sviluppo": "One of: pre-product / MVP / private beta / live product / active revenue / profitable"
  }},

  "team": {{
    "fondatori": [
      {{
        "nome": "EXACT name as written in the deck — never translate or anglicize",
        "ruolo": "...",
        "background": "Specific relevant experience. Where they worked, what they built, why they are the right person for this problem."
      }}
    ],
    "valutazione_team": "Critical assessment: does the team have domain expertise? Have they worked together before? Are key profiles missing (e.g. CTO, sales)? Is this a Series A-caliber team?"
  }},

  "mercato": {{
    "settore": "Sector of the END MARKET the startup serves (i.e. who are the paying customers). NOT the technology used internally. A startup using AI to sell to robot manufacturers is in 'Industrial Robotics', not 'Artificial Intelligence'.",
    "sottosettore": "Specific vertical or niche within the end market",
    "dimensione_mercato": "TAM/SAM/SOM if declared with source. If not declared, provide a reasoned qualitative estimate.",
    "tasso_di_crescita": "CAGR or growth trend if declared or estimable",
    "struttura_della_catena_del_valore": [
      {{
        "fase": "Nome preciso e specifico della fase (es. 'Originazione del credito' non 'Credito')",
        "descrizione": "2-3 frasi dense: cosa produce/scambia questa fase, quale ruolo ha nell'ecosistema, come si è evoluta negli ultimi 5 anni, quali tensioni strutturali la caratterizzano oggi",
        "player": ["Nome player (ruolo/dimensione)", "Nome player (ruolo/dimensione)", "Nome player (ruolo/dimensione)"],
        "margine": "2-3 frasi: chi cattura margine qui e perché — è strutturale o temporaneo? Quali leve usano (dati proprietari, network effect, lock-in, regolazione)? Il margine si sta erodendo o consolidando?",
        "posizione_startup": false
      }}
    ],
    "posizionamento_nella_catena": "Based on the value chain you built independently above, precisely position this company: which layer(s) does it operate in, who does it depend on upstream, who does it serve or sell to downstream, is it a B2B enabler or does it serve the end customer, does it own one layer or attempt vertical integration, where does it capture margin today vs where could it in the future, and which players in the chain could disintermediate it or replicate its function.",
    "dipendenze_strategiche": "Which players/platforms/data does it depend on? What is the risk if that player changes conditions?",
    "driver_di_mercato": "Which macro-trends or regulatory changes are creating the right timing for this solution?"
  }},

  "competizione": {{
    "player_globali": [
      {{"nome": "...", "descrizione": "What they do, how large they are, how they overlap with this company, how they differ"}}
    ],
    "player_europei": [
      {{"nome": "...", "descrizione": "What they do, how large they are, how they overlap with this company, how they differ"}}
    ],
    "vantaggio_competitivo_dichiarato": "What the founder claims differentiates them",
    "valutazione_critica_del_vantaggio": "Is the advantage real and defensible? Is it temporary or structural? How hard is it to replicate for a player with more resources?"
  }},

  "domande_per_il_founder": ["question 1", "question 2", "question 3", "question 4", "question 5", "question 6", "question 7"],

  "punti_di_attenzione": [
    {{
      "area": "e.g. Competition / Team / Technology / Market / Financials / Regulatory / etc.",
      "gravità": "High / Medium / Low",
      "descrizione": "Precise description of the risk or gap. Why it is a problem. What the founder should clarify or demonstrate to mitigate it."
    }}
  ],

  "sintesi": "3-4 sentences a VC partner would say to their team after reading the deck: why it could be interesting, what the 2 main risks are, and what question needs to be answered before proceeding."
}}"""

# ---------------------------------------------------------------------------
# HTML template  — React + Lovable design
# Note: uses str.replace() not .format(), so no {{ }} escaping needed.
# Placeholders: __ANALYSIS_JSON__  __WEB_USED__  __PDF_FILENAME__
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
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
    ['business','Business'],['prodotto','Product'],['team','Team'],
    ['mercato','Market'],['competizione','Competition'],
    ['domande','Questions'],['rischi','Red Flags'],
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
  const date = new Date().toLocaleDateString('en-GB',{day:'numeric',month:'long',year:'numeric'});
  return (
    <div style={{padding:'40px 0 28px',borderBottom:`1px solid ${C.border}`,marginBottom:28}}>
      <div style={{fontSize:11,fontWeight:700,letterSpacing:'.08em',textTransform:'uppercase',
        color:C.t3,marginBottom:10}}>Pitch Deck Analysis</div>
      <h1 style={{fontSize:38,fontWeight:700,letterSpacing:'-.8px',color:C.t1,
        lineHeight:1.15,marginBottom:8}}>{nome}</h1>
      {tag && <div style={{fontSize:16,color:C.t2,marginBottom:12,fontStyle:'italic'}}>"{tag}"</div>}
      <div style={{display:'flex',flexWrap:'wrap',gap:6,alignItems:'center',marginTop:8}}>
        <Pill>{date}</Pill>
        <Pill>GPT-4o</Pill>
        {web && <Pill teal>+ web research</Pill>}
      </div>
    </div>
  );
}

function Synthesis() {
  return (
    <div style={{background:C.dark,borderRadius:16,padding:'28px 30px',marginBottom:16}}>
      <div style={{fontSize:10,fontWeight:700,letterSpacing:'.1em',textTransform:'uppercase',
        color:'rgba(255,255,255,.4)',marginBottom:10}}>Sintesi</div>
      <p style={{fontSize:15,lineHeight:1.8,color:'rgba(255,255,255,.9)'}}>{D.sintesi}</p>
    </div>
  );
}

function BusinessSection() {
  const b = D.business||{};
  return (
    <Card id="business">
      <CardTitle>Business</CardTitle>
      <Field label="Problem">{b.problema}</Field>
      <Field label="Solution">{b.soluzione}</Field>
      <Field label="Business Model" mt={0}>{b.modello_di_business}</Field>
    </Card>
  );
}

function ProductSection() {
  const p = D.prodotto_tecnologia||{};
  const feats = p.caratteristiche_chiave||[];
  return (
    <Card id="prodotto">
      <CardTitle>Product & Technology</CardTitle>
      <Field label="Description">{p.descrizione}</Field>
      <Field label="Key Features">
        <div style={{display:'flex',flexWrap:'wrap',gap:4,marginTop:4}}>
          {feats.map((f,i)=><Tag key={i} v="green">{f}</Tag>)}
        </div>
      </Field>
      <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:20}}>
        <Field label="Tech Stack">{p.stack_tecnologico}</Field>
        <Field label="Stage"><Tag v="orange">{p.stadio_di_sviluppo}</Tag></Field>
      </div>
      <Field label="Tech Differentiator" mt={4}>{p.differenziatore_tecnologico}</Field>
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
      <Field label="Critical Assessment" mt={16}>{t.valutazione_team}</Field>
    </Card>
  );
}

function ValueChain({ fasi }) {
  if (!fasi || !Array.isArray(fasi) || fasi.length === 0) return null;
  return (
    <div style={{overflowX:'auto',paddingBottom:8,paddingTop:20}}>
      <div style={{display:'flex',alignItems:'stretch',gap:0,minWidth:'max-content'}}>
        {fasi.map((f,i) => (
          <React.Fragment key={i}>
            <div style={{
              position:'relative',
              background: f.posizione_startup ? C.blueBg : C.surface,
              border: `1.5px solid ${f.posizione_startup ? C.blue : C.border}`,
              borderRadius:12, padding:'14px 16px',
              minWidth:180, maxWidth:240,
            }}>
              {f.posizione_startup && (
                <div style={{
                  position:'absolute',top:-18,left:'50%',transform:'translateX(-50%)',
                  background:C.blue,color:'white',borderRadius:99,
                  padding:'2px 10px',fontSize:9,fontWeight:700,whiteSpace:'nowrap'
                }}>★ here</div>
              )}
              <div style={{
                fontWeight:700,fontSize:13,marginBottom:6,
                color: f.posizione_startup ? C.blue : C.t1
              }}>{f.fase}</div>
              {f.descrizione && (
                <div style={{fontSize:11,color:C.t2,lineHeight:1.5,marginBottom:8}}>{f.descrizione}</div>
              )}
              {f.margine && (
                <div style={{fontSize:10,color:C.t2,lineHeight:1.4,marginBottom:8,
                  background:'rgba(0,0,0,.04)',borderRadius:6,padding:'5px 7px'}}>
                  <span style={{fontWeight:700,color:C.t1}}>Margine: </span>{f.margine}
                </div>
              )}
              <div style={{display:'flex',flexWrap:'wrap',gap:3}}>
                {(f.player||[]).map((p,j) => (
                  <span key={j} style={{
                    background: f.posizione_startup ? 'rgba(59,130,246,.1)' : '#f4f4f5',
                    color: f.posizione_startup ? C.blue : C.t3,
                    borderRadius:4,padding:'2px 7px',fontSize:10,fontWeight:500
                  }}>{p}</span>
                ))}
              </div>
            </div>
            {i < fasi.length-1 && (
              <div style={{display:'flex',alignItems:'center',padding:'0 6px',
                color:C.t3,fontSize:22,flexShrink:0}}>→</div>
            )}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

function MarketSection() {
  const m = D.mercato||{};
  const fasi = Array.isArray(m.struttura_della_catena_del_valore)
    ? m.struttura_della_catena_del_valore : [];
  return (
    <Card id="mercato">
      <CardTitle>Market</CardTitle>
      <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:24}}>
        <div>
          <Field label="Sector">
            <Tag v="blue">{m.settore}</Tag>
            <Tag v="gray">{m.sottosettore}</Tag>
          </Field>
          <Field label="Market Size">{m.dimensione_mercato}</Field>
          <Field label="Growth Rate">{m.tasso_di_crescita}</Field>
          <Field label="Market Drivers">{m.driver_di_mercato}</Field>
        </div>
        <div>
          <Field label="Value Chain Position">{m.posizionamento_nella_catena}</Field>
          <Field label="Strategic Dependencies">{m.dipendenze_strategiche}</Field>
        </div>
      </div>
      {fasi.length > 0 && (
        <div style={{marginTop:20,borderTop:`1px solid ${C.border}`,paddingTop:20}}>
          <div style={{fontSize:10,fontWeight:700,letterSpacing:'.07em',
            textTransform:'uppercase',color:C.t3,marginBottom:2}}>
            Industry Value Chain
          </div>
          <ValueChain fasi={fasi} />
        </div>
      )}
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
      <CardTitle>Competitive Arena</CardTitle>
      <Field label="Claimed Advantage">{c.vantaggio_competitivo_dichiarato}</Field>
      <Field label="Critical Assessment">{c.valutazione_critica_del_vantaggio}</Field>
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
      <CardTitle>Questions for the Founder</CardTitle>
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
      <CardTitle>Red Flags</CardTitle>
      {flags.length===0
        ? <p style={{color:C.t3,fontSize:14}}>No critical issues identified.</p>
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

def _pdf_to_base64_images(pdf_path: Path, max_pages: int = 25) -> list[str]:
    """Converte le pagine di un PDF in immagini JPEG base64."""
    doc = pdfium.PdfDocument(str(pdf_path))
    images = []
    for i in range(min(len(doc), max_pages)):
        page = doc[i]
        bitmap = page.render(scale=2.0)
        pil_img = bitmap.to_pil().convert("RGB")
        buf = BytesIO()
        pil_img.save(buf, format="JPEG", quality=85)
        images.append(base64.b64encode(buf.getvalue()).decode())
    return images


def vision_full_analysis(pdf_path: Path, client: OpenAI) -> dict:
    """Analisi completa via Vision per PDF image-based: manda le slide direttamente a GPT-4o."""
    print("  → PDF image-based, analisi diretta con GPT-4o Vision...")
    images = _pdf_to_base64_images(pdf_path)
    if not images:
        return {}

    content: list = [{
        "type": "text",
        "text": (
            "Sei un partner VC con 15 anni di esperienza. Analizza questo pitch deck "
            "(le slide ti vengono mostrate come immagini) e restituisci SOLO un JSON "
            "con la struttura esatta che segue. Tutto in italiano. "
            "Se un'informazione non è presente scrivi 'Non dichiarato nel deck'.\n"
            "IMPORTANTE: per struttura_della_catena_del_valore ignora il deck e costruisci "
            "la catena del valore del settore in autonomia: almeno 4-6 fasi, player reali nominati "
            "per ogni fase, flussi economici tra i nodi, dove si concentra il potere/margine. "
            "Il deck serve solo per determinare posizionamento_nella_catena.\n\n"
            + PHASE2_PROMPT.split("Produci un'analisi approfondita")[1].split("{{")[0].strip()
            + "\n\n"
            + "{\n"
            '  "nome_azienda": "...",\n'
            '  "tagline": "...",\n'
            '  "business": {"problema":"...","soluzione":"...","modello_di_business":"..."},\n'
            '  "prodotto_tecnologia": {"descrizione":"...","caratteristiche_chiave":["..."],'
            '"stack_tecnologico":"...","differenziatore_tecnologico":"...","stadio_di_sviluppo":"..."},\n'
            '  "team": {"fondatori":[{"nome":"...","ruolo":"...","background":"..."}],'
            '"valutazione_team":"..."},\n'
            '  "mercato": {"settore":"...","sottosettore":"...","dimensione_mercato":"...",'
            '"tasso_di_crescita":"...","struttura_della_catena_del_valore":[{"fase":"...","descrizione":"...","player":["..."],"margine":"...","posizione_startup":false}],'
            '"posizionamento_nella_catena":"...","dipendenze_strategiche":"...","driver_di_mercato":"..."},\n'
            '  "competizione": {"player_globali":[{"nome":"...","descrizione":"..."}],'
            '"player_europei":[{"nome":"...","descrizione":"..."}],'
            '"vantaggio_competitivo_dichiarato":"...","valutazione_critica_del_vantaggio":"..."},\n'
            '  "domande_per_il_founder": ["...","...","...","...","...","...","..."],\n'
            '  "punti_di_attenzione": [{"area":"...","gravità":"Alta/Media/Bassa","descrizione":"..."}],\n'
            '  "sintesi": "..."\n'
            "}"
        )
    }]
    for b64 in images:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}
        })

    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        response_format={"type": "json_object"},
        max_tokens=4000,
        temperature=0.2,
    )
    return json.loads(resp.choices[0].message.content)


def clean_text(text: str) -> str:
    """Rimuove artefatti di encoding PDF come (cid:20)."""
    import re
    text = re.sub(r'\(cid:\d+\)', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_text_from_pdf(pdf_path: Path) -> tuple[str, int]:
    """Estrae testo dal PDF. Restituisce (testo, numero_pagine)."""
    pages = []
    page_count = 0
    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            text = page.extract_text()
            if text and text.strip():
                pages.append(f"[Slide {i+1}]\n{clean_text(text)}")
    return "\n\n".join(pages), page_count


def is_image_based(text: str, page_count: int) -> bool:
    """True se il PDF ha meno di 100 caratteri per pagina in media."""
    return (len(text.strip()) / max(page_count, 1)) < 100

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
        max_tokens=16000,
    )
    raw = resp.choices[0]
    if raw.finish_reason == "length":
        print("⚠️  Risposta troncata da GPT-4o — considera di ridurre la lunghezza del deck")
    return json.loads(raw.message.content)

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
    client = OpenAI()
    pdf_text, page_count = extract_text_from_pdf(pdf_path)

    web_ctx = ""
    web_used = False

    if is_image_based(pdf_text, page_count):
        print("  → PDF image-based, analisi diretta con GPT-4o Vision...")
        data = vision_full_analysis(pdf_path, client)
        nome = data.get("nome_azienda", "Startup")
    else:
        print(f"  → {len(pdf_text)} caratteri estratti")
        print("Estraggo informazioni base...")
        meta = phase1_extract(client, pdf_text)
        nome = meta.get("nome_azienda", "Startup")
        sito = meta.get("sito_web") or ""
        settore = meta.get("settore", "")
        print(f"  → Azienda: {nome} | Settore: {settore}")

        if not args.no_web:
            print("Ricerca informazioni sul web...")
            web_ctx = web_research(nome, sito, settore)
            web_used = bool(web_ctx and "Nessuna" not in web_ctx)

        print("Analisi approfondita con GPT-4o...")
        data = phase2_analyze(client, nome, pdf_text, web_ctx)

    print(f"  → Analisi completata: {nome}")

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

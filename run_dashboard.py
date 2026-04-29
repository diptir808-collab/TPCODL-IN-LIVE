# -*- coding: utf-8 -*-
"""
TPCODL Dashboard — GitHub Actions runner
Runs once per trigger: login → download → build dashboard → push to GitHub Pages
No Flask, no schedule, no FTP. Pure and simple.
"""

import os, re, time, glob, logging, json, subprocess, shutil
import pandas as pd
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options

# ===============================================================
#  CONFIGURATION — secrets come from GitHub Actions environment
# ===============================================================
CONFIG = {
    "url":           "https://kavach.tpodisha.com/LoginPage",
    "username":      os.environ.get("TPCODL_USER", ""),
    "password":      os.environ.get("TPCODL_PASS", ""),
    "download_dir":  "downloads",
    "dashboard_file":"downloads/index.html",
    "reports":       ["PTW 11KV", "Tripping 11KV"],
    "github_token":  os.environ.get("GITHUB_TOKEN_PAT", ""),
    "github_repo":   os.environ.get("GITHUB_REPO", ""),
    "github_branch": os.environ.get("GITHUB_BRANCH", "gh-pages"),
    "public_url":    "",   # filled below after repo is known
}
CONFIG["public_url"] = "https://{}.github.io/{}/".format(
    CONFIG["github_repo"].split("/")[0] if "/" in CONFIG["github_repo"] else "",
    CONFIG["github_repo"].split("/")[1] if "/" in CONFIG["github_repo"] else CONFIG["github_repo"]
)

os.makedirs(CONFIG["download_dir"], exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ===============================================================
#  DATA HELPERS  (identical to your working local script)
# ===============================================================
def get_column(df, keywords):
    if df is None or df.empty:
        return None
    for col in df.columns:
        for kw in keywords:
            if kw.upper() in col.upper():
                return col
    return None

def get_col_by_index(df, col_index, fallback_keywords=None):
    if df is None or df.empty:
        return None
    cols = list(df.columns)
    if col_index < len(cols):
        col = cols[col_index]
        log.info(f"Column index {col_index} -> '{col}'")
        return col
    if fallback_keywords:
        return get_column(df, fallback_keywords)
    return None

def assign_shift(dt):
    if dt is None or pd.isnull(dt): return "Unknown"
    t = dt.hour * 60 + dt.minute
    if   7*60 <= t <= 14*60+30:  return "A"
    elif 14*60+31 <= t <= 22*60: return "B"
    else:                         return "C"

def load_ptw_data(file_path):
    df       = pd.read_excel(file_path)
    date_col = get_column(df, ['PTW ISSUED DATE'])
    time_col = get_column(df, ['PTW ISSUED TIME'])
    if date_col and time_col:
        df['datetime'] = pd.to_datetime(
            df[date_col].astype(str) + ' ' + df[time_col].astype(str),
            format='mixed', dayfirst=True, errors='coerce'
        )
        df = df.dropna(subset=['datetime'])
        df['shift'] = df['datetime'].apply(assign_shift)
        df['hour']  = df['datetime'].dt.hour
    return df

def load_tripping_data(file_path):
    df     = pd.read_excel(file_path)
    dt_col = get_column(df, ['INTERRUPTION START TIME', 'START TIME'])
    if dt_col:
        df['start_dt'] = pd.to_datetime(df[dt_col], format='mixed', dayfirst=True, errors='coerce')
        end_col = get_column(df, ['INTERRUPTION END TIME', 'END TIME', 'RESTORATION TIME', 'RECOVERY TIME'])
        if end_col:
            df['end_dt']       = pd.to_datetime(df[end_col], format='mixed', dayfirst=True, errors='coerce')
            df['duration_min'] = (df['end_dt'] - df['start_dt']).dt.total_seconds() / 60.0
        df = df.dropna(subset=['start_dt'])
        df['shift'] = df['start_dt'].apply(assign_shift)
        df['hour']  = df['start_dt'].dt.hour
    return df

def df_to_json_safe(df):
    if df.empty: return "[]"
    d = df.copy()
    for col in d.select_dtypes(include=['datetime64[ns]', 'datetimetz']).columns:
        d[col] = d[col].astype(str)
    return d.where(pd.notnull(d), other=None).to_json(orient='records', force_ascii=False)

# ===============================================================
#  DASHBOARD GENERATOR  (identical to your working local script)
# ===============================================================
def generate_dashboard(ptw_file, trip_file, output_html):
    ptw_df  = load_ptw_data(ptw_file)      if ptw_file  and os.path.exists(ptw_file)  else pd.DataFrame()
    trip_df = load_tripping_data(trip_file) if trip_file and os.path.exists(trip_file) else pd.DataFrame()

    circle_col_p  = get_column(ptw_df,  ['CIRCLE NAME', 'CIRCLE'])
    circle_col_t  = get_column(trip_df, ['CIRCLE NAME', 'CIRCLE'])
    div_col_p     = get_column(ptw_df,  ['DIVISION NAME', 'DIVISION'])
    div_col_t     = get_column(trip_df, ['DIVISION NAME', 'DIVISION'])
    status_col_p  = get_column(ptw_df,  ['STATUS'])
    status_col_t  = get_column(trip_df, ['STATUS'])
    iso_col_p     = get_col_by_index(ptw_df,  31, ['ISOLATION TYPE', 'ISOLATION'])
    iso_col_t     = get_column(trip_df, ['ISOLATION TYPE', 'ISOLATION'])
    gss_col       = get_column(ptw_df,  ['GSS/PSS NAME', 'GSS NAME', 'PSS NAME', 'GSS'])
    outage_col    = get_column(ptw_df,  ['OUTAGE TYPE'])
    cons_col_p    = get_col_by_index(ptw_df,  32, ['NO. OF CONS. AFFECTED', 'NO OF CONS', 'CONS. AFFECTED', 'CONSUMER', 'CUSTOMER', 'AFFECTED'])
    cons_col_t    = get_col_by_index(trip_df, 31, ['TOTAL CONNECTED CONSUMERS', 'TOTAL CONNECTED', 'CONNECTED CONSUMERS', 'CONSUMER', 'CUSTOMER', 'AFFECTED'])
    mw_col_p      = get_column(ptw_df,  ['MW', 'LOAD', 'DEMAND'])
    mw_col_t      = get_column(trip_df, ['MW', 'LOAD', 'DEMAND'])

    circles   = sorted(ptw_df[circle_col_p].dropna().unique().tolist()) if circle_col_p and not ptw_df.empty else []
    divisions = sorted(ptw_df[div_col_p].dropna().unique().tolist())    if div_col_p    and not ptw_df.empty else []
    iso_types = sorted(ptw_df[iso_col_p].dropna().unique().tolist())    if iso_col_p    and not ptw_df.empty else []

    ptw_json  = df_to_json_safe(ptw_df)
    trip_json = df_to_json_safe(trip_df)

    col_cfg = json.dumps({
        "p_circle": circle_col_p or "", "t_circle": circle_col_t or "",
        "p_div":    div_col_p    or "", "t_div":    div_col_t    or "",
        "p_status": status_col_p or "", "t_status": status_col_t or "",
        "p_iso":    iso_col_p    or "", "t_iso":    iso_col_t    or "",
        "p_gss":    gss_col      or "", "p_outage": outage_col   or "",
        "p_cons":   cons_col_p   or "", "t_cons":   cons_col_t   or "",
        "p_mw":     mw_col_p     or "", "t_mw":     mw_col_t     or "",
        "p_shift":  "shift",            "t_shift":  "shift",
        "p_hour":   "hour",             "t_hour":   "hour",
        "t_dur":    "duration_min",
    })

    circle_btns = '<button class="cbtn active" onclick="setCircle(this,\'ALL\')">ALL CIRCLES</button>\n'
    for c in circles:
        safe = c.replace("'", "\\'")
        circle_btns += f'<button class="cbtn" onclick="setCircle(this,\'{safe}\')">{c}</button>\n'

    last_update    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    public_url     = CONFIG["public_url"]
    ptw_show_cols  = [c for c in ptw_df.columns  if c not in ('shift','hour','datetime','start_dt','end_dt','duration_min')][:12]
    trip_show_cols = [c for c in trip_df.columns if c not in ('shift','hour','datetime','start_dt','end_dt')][:12]
    ptw_modal_cols  = json.dumps(ptw_show_cols)
    trip_modal_cols = json.dumps(trip_show_cols)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="300">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TPCODL Live Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',Arial,sans-serif;background:#eef2f7;color:#333}}
header{{background:linear-gradient(90deg,#1a237e,#283593);color:#fff;padding:14px 26px;display:flex;align-items:center;gap:14px}}
header h1{{font-size:1.4em;font-weight:700}}
header .sub{{font-size:.8em;opacity:.8;margin-top:3px}}
.live-badge{{background:#e8f5e9;color:#2e7d32;border:1px solid #a5d6a7;border-radius:20px;padding:2px 10px;font-size:.72em;margin-left:8px}}
.fbar{{background:#fff;padding:12px 22px;box-shadow:0 2px 6px rgba(0,0,0,.08);display:flex;flex-wrap:wrap;gap:10px;align-items:center;border-bottom:3px solid #e8eaf6}}
.fbar label{{font-weight:700;font-size:.82em;color:#1a237e;margin-right:4px;white-space:nowrap}}
.cbtn{{padding:5px 13px;border:2px solid #1a237e;border-radius:20px;background:#fff;color:#1a237e;cursor:pointer;font-size:.8em;font-weight:600;transition:all .15s}}
.cbtn.active{{background:#1a237e;color:#fff}}
.cbtn:hover{{background:#283593;color:#fff}}
.fsel{{padding:6px 10px;border:2px solid #1a237e;border-radius:8px;font-size:.8em;color:#1a237e;cursor:pointer;outline:none;background:#fff}}
.fsep{{width:1px;height:28px;background:#ddd;margin:0 4px}}
.wrap{{max-width:1750px;margin:20px auto;padding:0 16px}}
.kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:22px}}
.kpi{{background:#fff;border-radius:10px;padding:18px 14px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.09);border-top:4px solid;cursor:pointer;transition:transform .15s,box-shadow .15s;position:relative}}
.kpi:hover{{transform:translateY(-3px);box-shadow:0 6px 18px rgba(0,0,0,.14)}}
.kpi .click-hint{{position:absolute;top:6px;right:8px;font-size:.65em;color:#aaa;font-style:italic}}
.kpi.blue{{border-color:#1565c0}}.kpi.red{{border-color:#c62828}}.kpi.green{{border-color:#2e7d32}}
.kpi.orange{{border-color:#e65100}}.kpi.purple{{border-color:#6a1b9a}}.kpi.teal{{border-color:#00695c}}
.kpi.brown{{border-color:#4e342e}}.kpi.lime{{border-color:#827717}}.kpi.pink{{border-color:#880e4f}}
.kpi .val{{font-size:2em;font-weight:700;line-height:1.2}}
.kpi .lbl{{font-size:.71em;color:#666;margin-top:5px;text-transform:uppercase;letter-spacing:.5px}}
.chart-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(480px,1fr));gap:18px;margin-bottom:22px}}
.card{{background:#fff;border-radius:10px;padding:14px;box-shadow:0 2px 8px rgba(0,0,0,.08);overflow:hidden}}
.card h3{{font-size:.9em;color:#1a237e;margin-bottom:8px;padding-bottom:7px;border-bottom:2px solid #e8eaf6}}
.modal-bg{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;align-items:flex-start;justify-content:center;padding-top:60px}}
.modal-bg.open{{display:flex}}
.modal{{background:#fff;border-radius:12px;width:92vw;max-width:1100px;max-height:80vh;display:flex;flex-direction:column;box-shadow:0 8px 40px rgba(0,0,0,.25)}}
.modal-head{{background:#1a237e;color:#fff;padding:14px 20px;border-radius:12px 12px 0 0;display:flex;justify-content:space-between;align-items:center}}
.modal-head h2{{font-size:1em;font-weight:700}}
.modal-close{{background:none;border:none;color:#fff;font-size:1.4em;cursor:pointer;line-height:1;padding:0 4px}}
.modal-body{{overflow:auto;padding:16px}}
.modal-search input{{width:100%;padding:7px 12px;border:1px solid #ccc;border-radius:6px;font-size:.85em;outline:none;margin-bottom:10px}}
.dtable{{width:100%;border-collapse:collapse;font-size:.8em}}
.dtable th{{background:#e8eaf6;color:#1a237e;padding:7px 10px;text-align:left;position:sticky;top:0;white-space:nowrap}}
.dtable td{{padding:6px 10px;border-bottom:1px solid #f0f0f0;white-space:nowrap}}
.dtable tr:hover td{{background:#f5f7fa}}
.modal-footer{{padding:10px 16px;border-top:1px solid #eee;font-size:.78em;color:#888;display:flex;justify-content:space-between;align-items:center}}
footer{{text-align:center;padding:16px;color:#888;font-size:.78em}}
</style>
</head>
<body>
<header>
  <div>⚡</div>
  <div>
    <h1>TPCODL LIVE DASHBOARD <span class="live-badge">● LIVE</span></h1>
    <div class="sub">Updates every 5 min via GitHub Actions &nbsp;|&nbsp; Last updated: {last_update}</div>
  </div>
</header>
<div class="fbar">
  <label>🔵 Circle:</label>
  {circle_btns}
  <div class="fsep"></div>
  <label>📂 Division:</label>
  <select class="fsel" id="divSel" onchange="applyFilters()"><option value="ALL">All Divisions</option></select>
  <div class="fsep"></div>
  <label>🔌 Isolation Type:</label>
  <select class="fsel" id="isoSel" onchange="applyFilters()"><option value="ALL">All Types</option></select>
</div>
<div class="wrap">
  <div class="kpi-grid">
    <div class="kpi blue"   onclick="openModal('ptw_all')"><span class="click-hint">click to view ▼</span><div class="val" id="kTotalPTW">0</div><div class="lbl">Total PTWs</div></div>
    <div class="kpi pink"   onclick="openModal('ptw_live')"><span class="click-hint">click to view ▼</span><div class="val" id="kLivePTW">0</div><div class="lbl">🔴 Live PTWs (Issued)</div></div>
    <div class="kpi red"    onclick="openModal('trip_all')"><span class="click-hint">click to view ▼</span><div class="val" id="kTotalTrip">0</div><div class="lbl">Total Trippings</div></div>
    <div class="kpi lime"   onclick="openModal('trip_live')"><span class="click-hint">click to view ▼</span><div class="val" id="kLiveTrip">0</div><div class="lbl">🔴 Live Trippings (Live)</div></div>
    <div class="kpi orange" onclick="openModal('consumers')"><span class="click-hint">click to view ▼</span><div class="val" id="kConsumers">0</div><div class="lbl">Consumers Affected (Live)</div></div>
    <div class="kpi green"  onclick="openModal('mw')"><span class="click-hint">click to view ▼</span><div class="val" id="kLoadMW">0</div><div class="lbl">Load Loss MW (Live)</div></div>
    <div class="kpi teal"   onclick="openModal('trip_all')"><span class="click-hint">click to view ▼</span><div class="val" id="kAvgDur">0 min</div><div class="lbl">Avg Trip Duration</div></div>
    <div class="kpi brown"  onclick="openModal('ptw_all')"><span class="click-hint">click to view ▼</span><div class="val" id="kPeakHr">N/A</div><div class="lbl">Peak Hour</div></div>
  </div>
  <div class="chart-grid">
    <div class="card"><h3>Hourly PTW Trend</h3><div id="cHourly" style="height:300px"></div></div>
    <div class="card"><h3>Outage Type Trend over Hours</h3><div id="cOutageTrend" style="height:300px"></div></div>
  </div>
  <div class="chart-grid">
    <div class="card"><h3>Top 10 GSS by PTW</h3><div id="cGSS" style="height:360px"></div></div>
    <div class="card"><h3>Top 5 Divisions by PTW</h3><div id="cDiv" style="height:360px"></div></div>
  </div>
  <div class="chart-grid">
    <div class="card"><h3>Outage Type Distribution</h3><div id="cPie" style="height:340px"></div></div>
    <div class="card" style="display:flex;flex-direction:column;justify-content:center;align-items:center;gap:10px;padding:28px;">
      <div style="font-size:2.2em">📡</div>
      <div style="font-weight:700;color:#1a237e">Live Dashboard</div>
      <div style="color:#555;text-align:center;line-height:1.9;font-size:.88em">
        Data updates every <strong>5 minutes</strong><br>
        Powered by <strong>GitHub Actions</strong><br>
        <a href="{public_url}" style="color:#1565c0">{public_url}</a>
      </div>
    </div>
  </div>
</div>
<footer>TPCODL Shift Dashboard &nbsp;|&nbsp; {last_update} &nbsp;|&nbsp; GitHub Actions Auto-Update</footer>
<div class="modal-bg" id="modalBg" onclick="bgClick(event)">
  <div class="modal">
    <div class="modal-head"><h2 id="modalTitle">Detail View</h2><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      <div class="modal-search"><input type="text" id="modalSearch" placeholder="🔍 Search in table…" oninput="filterModal()"></div>
      <div id="modalContent"></div>
    </div>
    <div class="modal-footer"><span id="modalCount"></span><span>Click outside or ✕ to close</span></div>
  </div>
</div>
<script>
const PTW_RAW={ptw_json};
const TRIP_RAW={trip_json};
const C={col_cfg};
const PTW_COLS={ptw_modal_cols};
const TRIP_COLS={trip_modal_cols};
let SEL_CIRCLE='ALL';
let _modalRows=[],_modalCols=[];
function byCircle(data,isTrip){{if(SEL_CIRCLE==='ALL')return data;const col=isTrip?C.t_circle:C.p_circle;return col?data.filter(r=>r[col]===SEL_CIRCLE):data;}}
function byDiv(data,isTrip){{const v=document.getElementById('divSel').value;if(v==='ALL')return data;const col=isTrip?C.t_div:C.p_div;return col?data.filter(r=>r[col]===v):data;}}
function byIso(data,isTrip){{const v=document.getElementById('isoSel').value;if(v==='ALL')return data;const col=isTrip?C.t_iso:C.p_iso;return col?data.filter(r=>r[col]===v):data;}}
function isIssued(r,isTrip){{const col=isTrip?C.t_status:C.p_status;if(!col)return false;const val=(r[col]||'').toString().toUpperCase().trim();return isTrip?val==='LIVE':val==='ISSUED';}}
function numSum(arr,col){{if(!col)return 0;return arr.reduce((s,r)=>s+(parseFloat(r[col])||0),0);}}
function fmt(n,d=0){{return n.toLocaleString('en-IN',{{maximumFractionDigits:d}});}}
function filteredPTW(){{return byIso(byDiv(byCircle(PTW_RAW,false),false),false);}}
function filteredTRIP(){{return byIso(byDiv(byCircle(TRIP_RAW,true),true),true);}}
function setCircle(btn,val){{SEL_CIRCLE=val;document.querySelectorAll('.cbtn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');rebuildDivDropdown();rebuildIsoDropdown();applyFilters();}}
function rebuildDivDropdown(){{const ptw=byCircle(PTW_RAW,false);const divs=[...new Set(ptw.map(r=>r[C.p_div]).filter(Boolean))].sort();const sel=document.getElementById('divSel');sel.innerHTML='<option value="ALL">All Divisions</option>';divs.forEach(d=>{{const o=document.createElement('option');o.value=d;o.text=d;sel.appendChild(o);}});}}
function rebuildIsoDropdown(){{const ptw=byDiv(byCircle(PTW_RAW,false),false);const types=[...new Set(ptw.map(r=>r[C.p_iso]).filter(Boolean))].sort();const sel=document.getElementById('isoSel');sel.innerHTML='<option value="ALL">All Types</option>';types.forEach(t=>{{const o=document.createElement('option');o.value=t;o.text=t;sel.appendChild(o);}});}}
function applyFilters(){{
  const ptw=filteredPTW();const trip=filteredTRIP();
  const livePTW=ptw.filter(r=>isIssued(r,false));const liveTrip=trip.filter(r=>isIssued(r,true));
  const consumers=numSum(livePTW,C.p_cons)+numSum(liveTrip,C.t_cons);
  const mw=numSum(livePTW,C.p_mw)+numSum(liveTrip,C.t_mw);
  let avgDur=0;
  if(C.t_dur&&trip.length>0){{const durs=trip.map(r=>parseFloat(r[C.t_dur])).filter(v=>!isNaN(v));avgDur=durs.length?durs.reduce((a,b)=>a+b,0)/durs.length:0;}}
  const hc={{}};ptw.forEach(r=>{{const h=r[C.p_hour];if(h!=null)hc[h]=(hc[h]||0)+1;}});
  let peakHr='N/A';const hEntries=Object.entries(hc);
  if(hEntries.length){{const top=hEntries.sort((a,b)=>b[1]-a[1])[0];peakHr=String(top[0]).padStart(2,'0')+':00 ('+top[1]+')';}}
  document.getElementById('kTotalPTW').textContent=fmt(ptw.length);
  document.getElementById('kLivePTW').textContent=fmt(livePTW.length);
  document.getElementById('kTotalTrip').textContent=fmt(trip.length);
  document.getElementById('kLiveTrip').textContent=fmt(liveTrip.length);
  document.getElementById('kConsumers').textContent=fmt(consumers);
  document.getElementById('kLoadMW').textContent=fmt(mw,2);
  document.getElementById('kAvgDur').textContent=fmt(avgDur,1)+' min';
  document.getElementById('kPeakHr').textContent=peakHr;
  drawCharts(ptw,trip);
}}
const COLORS=['#1565c0','#c62828','#2e7d32','#e65100','#6a1b9a','#00695c','#4e342e','#827717','#880e4f','#01579b'];
function countBy(arr,col){{const m={{}};arr.forEach(r=>{{const v=r[col]||'Unknown';m[v]=(m[v]||0)+1;}});return m;}}
function drawCharts(ptw,trip){{
  const hm={{}};ptw.forEach(r=>{{const h=r[C.p_hour];if(h!=null)hm[h]=(hm[h]||0)+1;}});
  const hk=Object.keys(hm).map(Number).sort((a,b)=>a-b);
  Plotly.react('cHourly',[{{type:'scatter',mode:'lines+markers',x:hk,y:hk.map(h=>hm[h]),line:{{color:'#1565c0',width:2}},marker:{{size:7}}}}],{{margin:{{t:10,b:40,l:40,r:10}},xaxis:{{title:'Hour'}},yaxis:{{title:'PTWs'}}}},{{responsive:true}});
  const traces=[];
  if(C.p_outage){{const types=[...new Set(ptw.map(r=>r[C.p_outage]).filter(Boolean))];types.forEach((ot,i)=>{{const om={{}};ptw.filter(r=>r[C.p_outage]===ot).forEach(r=>{{const h=r[C.p_hour];if(h!=null)om[h]=(om[h]||0)+1;}});const ok=Object.keys(om).map(Number).sort((a,b)=>a-b);traces.push({{type:'scatter',mode:'lines+markers',name:ot,x:ok,y:ok.map(h=>om[h]),marker:{{color:COLORS[i%COLORS.length]}}}});}});}}
  Plotly.react('cOutageTrend',traces.length?traces:[{{type:'scatter',x:[],y:[]}}],{{margin:{{t:10,b:40,l:40,r:10}},xaxis:{{title:'Hour'}},yaxis:{{title:'Count'}}}},{{responsive:true}});
  if(C.p_gss){{const gm=countBy(ptw,C.p_gss);const gs=Object.entries(gm).sort((a,b)=>b[1]-a[1]).slice(0,10);Plotly.react('cGSS',[{{type:'bar',x:gs.map(e=>e[1]),y:gs.map(e=>e[0]),orientation:'h',marker:{{color:'#1565c0'}}}}],{{margin:{{t:10,b:40,l:180,r:10}},xaxis:{{title:'PTWs'}}}},{{responsive:true}});}}
  if(C.p_div){{const dm=countBy(ptw,C.p_div);const ds=Object.entries(dm).sort((a,b)=>b[1]-a[1]).slice(0,5);Plotly.react('cDiv',[{{type:'bar',x:ds.map(e=>e[0]),y:ds.map(e=>e[1]),marker:{{color:COLORS}}}}],{{margin:{{t:10,b:80,l:40,r:10}},xaxis:{{tickangle:-20}},yaxis:{{title:'PTWs'}}}},{{responsive:true}});}}
  if(C.p_outage){{const om=countBy(ptw,C.p_outage);Plotly.react('cPie',[{{type:'pie',labels:Object.keys(om),values:Object.values(om),hole:.35,marker:{{colors:COLORS}}}}],{{margin:{{t:20,b:20,l:20,r:20}},showlegend:true}},{{responsive:true}});}}
}}
function openModal(type){{
  const ptw=filteredPTW();const trip=filteredTRIP();
  let rows=[],cols=[],title='';
  if(type==='ptw_all'){{rows=ptw;cols=PTW_COLS;title='All PTWs ('+rows.length+' records)';}}
  else if(type==='ptw_live'){{rows=ptw.filter(r=>isIssued(r,false));cols=PTW_COLS;title='Live PTWs — STATUS=ISSUED ('+rows.length+' records)';}}
  else if(type==='trip_all'){{rows=trip;cols=TRIP_COLS;title='All Trippings ('+rows.length+' records)';}}
  else if(type==='trip_live'){{rows=trip.filter(r=>isIssued(r,true));cols=TRIP_COLS;title='Live Trippings — STATUS=LIVE ('+rows.length+' records)';}}
  else if(type==='consumers'){{rows=[...ptw.filter(r=>isIssued(r,false)),...trip.filter(r=>isIssued(r,true))];cols=PTW_COLS;title='Consumers Affected — Live Records ('+rows.length+')';}}
  else if(type==='mw'){{rows=[...ptw.filter(r=>isIssued(r,false)),...trip.filter(r=>isIssued(r,true))];cols=PTW_COLS;title='Load Loss MW — Live Records ('+rows.length+')';}}
  _modalRows=rows;_modalCols=cols;
  document.getElementById('modalTitle').textContent=title;
  document.getElementById('modalSearch').value='';
  renderModalTable(rows,cols);
  document.getElementById('modalBg').classList.add('open');
}}
function renderModalTable(rows,cols){{
  if(!rows.length){{document.getElementById('modalContent').innerHTML='<p style="color:#888;padding:20px">No data for current filters.</p>';document.getElementById('modalCount').textContent='0 records';return;}}
  let h='<table class="dtable"><thead><tr>'+cols.map(c=>'<th>'+c+'</th>').join('')+'</tr></thead><tbody>';
  rows.slice(0,500).forEach(r=>{{h+='<tr>'+cols.map(c=>'<td>'+(r[c]!=null?r[c]:'')+'</td>').join('')+'</tr>';}});
  h+='</tbody></table>';
  document.getElementById('modalContent').innerHTML=h;
  document.getElementById('modalCount').textContent=rows.length+' records'+(rows.length>500?' (showing first 500)':'');
}}
function filterModal(){{
  const q=document.getElementById('modalSearch').value.toLowerCase();
  if(!q){{renderModalTable(_modalRows,_modalCols);return;}}
  const filtered=_modalRows.filter(r=>_modalCols.some(c=>String(r[c]||'').toLowerCase().includes(q)));
  renderModalTable(filtered,_modalCols);
}}
function closeModal(){{document.getElementById('modalBg').classList.remove('open');}}
function bgClick(e){{if(e.target===document.getElementById('modalBg'))closeModal();}}
window.onload=function(){{rebuildDivDropdown();rebuildIsoDropdown();applyFilters();}};
</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_html), exist_ok=True)
    with open(output_html, 'w', encoding='utf-8') as f:
        f.write(html)
    log.info(f"Dashboard saved → {output_html}")

# ===============================================================
#  GITHUB PAGES PUBLISHER
# ===============================================================
def _git(cmd, cwd="."):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"git error: {result.stderr.strip()}")
        raise RuntimeError(result.stderr.strip())
    return result

def publish_to_github(local_html):
    token  = CONFIG["github_token"]
    repo   = CONFIG["github_repo"]
    branch = CONFIG["github_branch"]
    rdir   = "repo"

    if not token or not repo:
        log.error("GITHUB_TOKEN_PAT or GITHUB_REPO not set")
        return False

    remote = f"https://x-access-token:{token}@github.com/{repo}.git"

    shutil.rmtree(rdir, ignore_errors=True)
    log.info(f"Cloning {repo} branch={branch} ...")
    subprocess.run(
        ["git", "clone", "--branch", branch, "--single-branch", "--depth=1", remote, rdir],
        check=True, capture_output=True, text=True
    )

    _git(["git", "config", "user.email", "gha-bot@noreply"], rdir)
    _git(["git", "config", "user.name",  "GHA Bot"], rdir)
    _git(["git", "remote", "set-url", "origin", remote], rdir)

    shutil.copy2(local_html, os.path.join(rdir, "index.html"))

    nojekyll = os.path.join(rdir, ".nojekyll")
    if not os.path.exists(nojekyll):
        open(nojekyll, "w").close()

    _git(["git", "add", "index.html", ".nojekyll"], rdir)

    status = _git(["git", "status", "--porcelain"], rdir)
    if not status.stdout.strip():
        log.info("No changes — dashboard already up to date")
        return True

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _git(["git", "commit", "-m", f"Auto-update {ts}"], rdir)
    _git(["git", "push", "origin", branch], rdir)
    log.info(f"Dashboard published → {CONFIG['public_url']}")
    return True

# ===============================================================
#  SELENIUM — Chrome with Xvfb (set by workflow before running)
# ===============================================================
def get_driver():
    opts = Options()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    prefs = {
        "download.default_directory":   os.path.abspath(CONFIG["download_dir"]),
        "download.prompt_for_download": False,
        "download.directory_upgrade":   True,
        "safebrowsing.enabled":         True,
    }
    opts.add_experimental_option("prefs", prefs)
    service = Service("/usr/bin/chromedriver")
    driver  = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(60)
    return driver

def solve_captcha(text):
    m = re.search(r'(\d+)\s*([\+\-\*\/])\s*(\d+)', str(text))
    return str(eval(m.group(1) + m.group(2) + m.group(3))) if m else ""

def login(driver, wait):
    log.info("Logging in ...")
    driver.get(CONFIG["url"])
    try:
        wait.until(EC.presence_of_element_located((By.ID, "ddlDiscom")))
        log.info("Login page loaded")
        time.sleep(2)

        # DISCOM — exact id from page inspect
        try:
            sel = Select(driver.find_element(By.ID, "ddlDiscom"))
            try:
                sel.select_by_visible_text("TPCODL")
            except:
                for opt in sel.options:
                    if "TPCODL" in opt.text.upper():
                        driver.execute_script("arguments[0].selected=true;", opt)
                        driver.execute_script("arguments[0].dispatchEvent(new Event('change'));",
                                              driver.find_element(By.ID, "ddlDiscom"))
                        break
            log.info("DISCOM = TPCODL selected")
            time.sleep(1)
        except Exception as e:
            log.warning(f"DISCOM: {e}")

        # User ID
        uid = driver.find_element(By.ID, "txtLogin")
        uid.clear(); uid.send_keys(CONFIG["username"])

        # Password
        pwd = driver.find_element(By.ID, "txtPassword")
        pwd.clear(); pwd.send_keys(CONFIG["password"])

        # Active Directory checkbox
        try:
            cb = driver.find_element(By.XPATH, "//input[@type='checkbox']")
            if not cb.is_selected():
                driver.execute_script("arguments[0].click();", cb)
            log.info("AD Auth checkbox ticked")
        except Exception as e:
            log.warning(f"Checkbox: {e}")
        time.sleep(0.3)

        # Captcha
        try:
            cap_text = ""
            for by, sel in [(By.ID, "lblCaptcha"),
                            (By.XPATH, "//*[contains(@id,'Captcha') or contains(@id,'captcha')]")]:
                try:
                    el = driver.find_element(by, sel)
                    cap_text = el.get_attribute("value") or el.text or ""
                    if cap_text.strip(): break
                except: continue
            if not cap_text:
                body = driver.find_element(By.TAG_NAME, "body").text
                m = re.search(r"(\d+)\s*([\+\-\*\/])\s*(\d+)", body)
                if m: cap_text = m.group(0)
            ans = solve_captcha(cap_text)
            if ans:
                cap_inp = driver.find_element(By.XPATH,
                    "//input[contains(@placeholder,'captcha') or contains(@placeholder,'Captcha') or contains(@id,'Captcha')]")
                cap_inp.clear(); cap_inp.send_keys(ans)
                log.info(f"Captcha: '{cap_text}' = {ans}")
        except Exception as e:
            log.warning(f"Captcha: {e}")
        time.sleep(0.3)

        # Submit
        try:
            btn = wait.until(EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(text(),'SUBMIT') or contains(text(),'Submit')]")))
            driver.execute_script("arguments[0].click();", btn)
            log.info("SUBMIT clicked")
        except:
            driver.find_element(By.ID, "txtPassword").send_keys(Keys.ENTER)

        time.sleep(6)

        if "LoginPage" in driver.current_url:
            try: driver.save_screenshot("downloads/login_failed.png")
            except: pass
            log.error("Login FAILED")
            return False

        log.info(f"Login OK → {driver.current_url}")
        return True

    except Exception as e:
        log.error(f"Login exception: {e}")
        try: driver.save_screenshot("downloads/login_error.png")
        except: pass
        return False

def download_report(driver, wait, report_type):
    today = datetime.now().strftime("%Y-%m-%d")
    log.info(f"Downloading {report_type} ...")
    try:
        rm = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//a[contains(@href,'#') and contains(.,'Reports')]")))
        ActionChains(driver).move_to_element(rm).click().perform()
        time.sleep(2)
        ro = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//ul[contains(@class,'dropdown-menu')]//a[normalize-space(text())='Reports']")))
        driver.execute_script("arguments[0].click();", ro)
        time.sleep(4)
        Select(wait.until(EC.presence_of_element_located(
            (By.ID, "MainContent_ddl_ptwtripping")))).select_by_visible_text(report_type)
        time.sleep(2)

        if "Tripping" in report_type:
            try:
                status_ddl = wait.until(EC.presence_of_element_located(
                    (By.XPATH, "//select[contains(@id,'Status') or contains(@id,'status') or contains(@id,'ddl_status') or contains(@id,'ddlStatus')]")))
                Select(status_ddl).select_by_visible_text("LIVE")
                log.info("Tripping STATUS = LIVE")
                time.sleep(1)
            except Exception as e:
                log.warning(f"STATUS LIVE failed: {e}")

        for fid, val in [("MainContent_txt_from_date", today), ("MainContent_txt_to_date", today)]:
            el = wait.until(EC.presence_of_element_located((By.ID, fid)))
            driver.execute_script("arguments[0].value=arguments[1];", el, val)
            driver.execute_script("arguments[0].dispatchEvent(new Event('change'));", el)
        time.sleep(2)
        for fid, val in [("MainContent_txt_from_time", "00:00"), ("MainContent_txt_to_time", "23:59")]:
            try:
                el = driver.find_element(By.ID, fid)
                driver.execute_script("arguments[0].value=arguments[1];", el, val)
                driver.execute_script("arguments[0].dispatchEvent(new Event('change'));", el)
            except: pass

        eb = wait.until(EC.element_to_be_clickable((By.ID, "MainContent_btnExport")))
        driver.execute_script("arguments[0].scrollIntoView(true);", eb)
        time.sleep(1)
        driver.execute_script("arguments[0].click();", eb)
        log.info("Export clicked — waiting for file ...")

        existing = set(glob.glob(os.path.join(CONFIG["download_dir"], "*.xls*")))
        deadline = time.time() + 120
        while time.time() < deadline:
            current = set(glob.glob(os.path.join(CONFIG["download_dir"], "*.xls*")))
            nf = [f for f in (current - existing) if not f.endswith(".crdownload")]
            if nf:
                latest = max(nf, key=os.path.getmtime)
                log.info(f"Downloaded → {latest}")
                return latest
            time.sleep(2)
        log.error(f"Timeout: {report_type}")
    except Exception as e:
        log.error(f"download_report error: {e}")
    return None

# ===============================================================
#  MAIN — runs once and exits
# ===============================================================
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("TPCODL GitHub Actions Job Started")
    log.info("=" * 60)

    driver = get_driver()
    wait   = WebDriverWait(driver, 45)

    try:
        if not login(driver, wait):
            driver.quit()
            raise SystemExit("Login failed — check credentials")

        # Clean old XLS files
        for f in glob.glob(os.path.join(CONFIG["download_dir"], "*.xls*")):
            try: os.remove(f)
            except: pass

        ptw_file = trip_file = None
        for report in CONFIG["reports"]:
            path = download_report(driver, wait, report)
            if path:
                if "PTW"      in report: ptw_file  = path
                if "Tripping" in report: trip_file = path
            else:
                log.error(f"FAILED to download: {report}")

        driver.quit()

        if ptw_file and trip_file:
            generate_dashboard(ptw_file, trip_file, CONFIG["dashboard_file"])
            publish_to_github(CONFIG["dashboard_file"])
            log.info("Job completed successfully")
        else:
            log.error("Missing download files — dashboard not updated")
            raise SystemExit("Download failed")

    except SystemExit:
        raise
    except Exception as e:
        log.error(f"Job error: {e}")
        try: driver.quit()
        except: pass
        raise SystemExit(str(e))

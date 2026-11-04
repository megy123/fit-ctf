"""Self-contained HTML view; all untrusted text is inserted with textContent."""
# ruff: noqa: E501

import json


def render_scoreboard_html(state):
    payload = (
        json.dumps(state, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    return _TEMPLATE.replace("__SCOREBOARD_STATE__", payload)


_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Attack &amp; Defense · Scoreboard</title>
<style>
:root{color-scheme:dark;font-family:system-ui,sans-serif;background:#0c111b;color:#e6edf7}
*{box-sizing:border-box}body{max-width:1200px;margin:auto;padding:32px 24px 56px}
header{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:28px}
h1{font-size:32px;margin:8px 0}h2{font-size:18px;margin:0 0 16px}p{color:#a6b6cc;line-height:1.5}
.eyebrow{color:#61dbce;font-size:12px;letter-spacing:.15em;text-transform:uppercase}
.badge{padding:8px 14px;border:1px solid #344155;border-radius:30px;white-space:nowrap}
.card{background:#141e2d;border:1px solid #29354a;border-radius:14px;padding:22px;margin:18px 0}
.scroll{overflow:auto}table{width:100%;border-collapse:collapse;white-space:nowrap;font-variant-numeric:tabular-nums}
th{text-align:left;color:#91a3bb;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
td,th{padding:15px 14px;border-bottom:1px solid #29354a}tbody tr:last-child td{border:0}
td:last-child{font-weight:700;color:#e3fbf5}.num{text-align:right}.first{background:#1b3040}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.metric{padding:18px;background:#141e2d;border:1px solid #29354a;border-radius:12px}
.metric small{display:block;color:#91a3bb;margin-bottom:10px}.metric strong{font-size:25px;font-variant-numeric:tabular-nums}
.muted{color:#91a3bb;font-size:13px}
td.service-status{color:#ff8c95;text-align:center;font-weight:700}
td.service-status.up{color:#69e3ba}
.services-matrix th:not(:first-child){text-align:center}
.services-matrix tbody th{text-transform:none;letter-spacing:normal;color:#e6edf7;font-size:inherit;font-weight:400}
#connection{font-size:13px;margin-top:8px}footer{margin-top:22px;color:#91a3bb;font-size:12px}
@media(max-width:700px){body{padding:20px 12px}.metrics{grid-template-columns:repeat(2,1fr)}header{align-items:flex-start}.card{padding:14px}h1{font-size:26px}}
</style></head><body>
<header><div><div class="eyebrow">Attack &amp; Defense / Live standings</div><h1 id="project"></h1><div id="connection" role="status"></div></div><span id="state" class="badge"></span></header>
<section class="card"><h2>Team scoreboard</h2><p id="formula" class="muted"></p><div class="scroll"><table aria-label="Team rankings"><thead><tr><th>Rank</th><th>Team</th><th class="num">Attack</th><th class="num">Defense</th><th class="num">SLA</th><th class="num">Total</th></tr></thead><tbody id="teams"></tbody></table></div><p id="scored" class="muted"></p></section>
<section class="metrics" aria-label="Tick progress"><div class="metric"><small>Current tick</small><strong id="tick"></strong></div><div class="metric"><small>Time left in tick</small><strong id="countdown"></strong></div><div class="metric"><small>Match time left · estimate</small><strong id="remaining"></strong></div></section>
<section class="card"><h2>Team services</h2><p id="last-check" class="muted"></p><div class="scroll"><table class="services-matrix" aria-label="Service health"><thead><tr id="service-columns"></tr></thead><tbody id="services"></tbody></table></div></section>
<footer id="updated"></footer>
<script type="application/json" id="initial-state">__SCOREBOARD_STATE__</script>
<script>
let data=JSON.parse(document.getElementById('initial-state').textContent), received=Date.now(), connected=false;
const offline=location.protocol==='file:';
const el=id=>document.getElementById(id), put=(id,text)=>el(id).textContent=text;
const number=n=>Number(n).toLocaleString('en-US',{maximumFractionDigits:3});
function duration(s){
 if(s==null)return '—';s=Math.max(0,Math.ceil(s));
 return [Math.floor(s/3600),Math.floor(s/60)%60,s%60].map(n=>String(n).padStart(2,'0')).join(':');
}
function cell(row,text,cls=''){let td=document.createElement('td');td.textContent=text;td.className=cls;row.append(td);return td;}
function clocks(){
 const t=data.tick, elapsed=connected?(Date.now()-received)/1000:0;
 put('countdown',duration(t.seconds_remaining==null?null:t.seconds_remaining-elapsed));
 const consumed=t.deadline?Math.min(elapsed,t.seconds_remaining??0):0;
 put('remaining',duration(t.match_seconds_remaining==null?null:t.match_seconds_remaining-consumed));
}
function render(){
 put('project',data.project);put('state',data.state);
 const c=data.coefficients,t=data.tick;
 put('formula',`Total = ${c.ma} × Attack + ${c.mb} × Defense + ${c.mc} × SLA.`);
 el('teams').replaceChildren();
 for(const team of data.teams){let tr=document.createElement('tr');if(team.rank===1)tr.className='first';cell(tr,team.rank);cell(tr,team.team);for(const k of ['attack','defense','sla'])cell(tr,number(team.weighted[k]),'num');cell(tr,number(team.total),'num');el('teams').append(tr);}
 if(!data.teams.length){let tr=document.createElement('tr');cell(tr,'No teams enrolled yet.');el('teams').append(tr);}
 put('scored',t.scored_through==null?'No completed tick yet. Scores start at zero.':`Scores finalized through tick ${t.scored_through+1}.`);
 put('tick',t.number==null?'—':`${t.number+1} / ${t.count??'∞'}`);
 renderServices();
 put('updated',`Data as of ${new Date(data.generated_at).toLocaleString()}. Service checks and official tick scoring run independently.`);clocks();
}
function renderServices(){
 const key=s=>JSON.stringify([s.cluster,s.node,s.service]);
 const columns=[...new Map(data.services.map(s=>[key(s),s])).entries()].sort(([a],[b])=>a.localeCompare(b));
 const lookup=new Map(data.services.map(s=>[JSON.stringify([s.team_id,key(s)]),s]));
 el('service-columns').replaceChildren();
 for(const label of ['Team',...columns.map(([,s])=>[s.cluster,s.node,s.service].filter(Boolean).join(' / '))]){
  const th=document.createElement('th');th.textContent=label;th.scope='col';el('service-columns').append(th);
 }
 el('services').replaceChildren();
 for(const team of data.teams){
  const tr=document.createElement('tr');const heading=document.createElement('th');heading.textContent=team.team;heading.scope='row';tr.append(heading);
  for(const [column] of columns){
   const service=lookup.get(JSON.stringify([team.team_id,column]));
   const status=service?.status??'unknown';cell(tr,status==='up'?'UP':'DOWN',status==='up'?'service-status up':'service-status down').title=status;
  }
  el('services').append(tr);
 }
 if(!columns.length){let tr=document.createElement('tr');cell(tr,'No services configured yet.');el('services').append(tr);}
 const checks=data.services.map(s=>s.checked_at).filter(Boolean).sort((a,b)=>Date.parse(a)-Date.parse(b));
 put('last-check','Last check: '+(checks.length?new Date(checks[checks.length-1]).toLocaleString():'Never'));
}
async function poll(){
 if(offline){put('connection','Offline snapshot · refresh disabled');return;}
 try{const response=await fetch('./scoreboard-state',{cache:'no-store'});if(!response.ok)throw Error('HTTP '+response.status);data=await response.json();received=Date.now();connected=true;render();put('connection','● Connected · refresh every '+data.refresh_seconds+'s');}
 catch(error){connected=false;put('connection','Connection lost · showing last snapshot');clocks();}
 setTimeout(poll,(data.refresh_seconds||2)*1000);
}
render();poll();setInterval(clocks,1000);
</script></body></html>"""

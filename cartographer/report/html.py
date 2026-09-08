"""
Self-contained interactive graph.

No CDN, no external stylesheet, no fonts: one HTML file that opens from disk on
a locked-down corporate laptop with no network. The force simulation is about
sixty lines of vanilla JS -- pulling in d3 would break the offline guarantee for
no real gain at this size.
"""
from __future__ import annotations

import json

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{color-scheme:light dark;
    --bg:#fbfbfa; --fg:#1a1a19; --muted:#6b6b68; --line:#d8d8d4;
    --panel:#fff; --accent:#3b6ea5; --warn:#b4661e;}
  @media (prefers-color-scheme:dark){:root{
    --bg:#16171a; --fg:#e8e8e6; --muted:#9a9a96; --line:#33343a;
    --panel:#1e1f23; --accent:#7aa8d6; --warn:#d99a5b;}}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
    font:13px/1.5 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif;}
  header{padding:10px 14px;border-bottom:1px solid var(--line);
    display:flex;gap:12px;align-items:center;flex-wrap:wrap;background:var(--panel)}
  h1{font-size:14px;margin:0;font-weight:600}
  .meta{color:var(--muted);font-size:12px}
  input,select,button{font:inherit;padding:4px 8px;border:1px solid var(--line);
    border-radius:5px;background:var(--bg);color:var(--fg)}
  button{cursor:pointer}
  #wrap{display:flex;height:calc(100vh - 52px)}
  #cv{flex:1;display:block;cursor:grab}
  #cv:active{cursor:grabbing}
  aside{width:330px;border-left:1px solid var(--line);overflow:auto;
    padding:12px;background:var(--panel)}
  aside h2{font-size:13px;margin:0 0 6px}
  .row{padding:5px 0;border-bottom:1px solid var(--line);font-size:12px}
  .k{color:var(--muted)}
  code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}
  .legend span{display:inline-flex;align-items:center;gap:5px;margin-right:10px;
    font-size:12px;color:var(--muted)}
  .dot{width:9px;height:9px;border-radius:50%}
  .hint{color:var(--muted);font-size:12px}
</style></head><body>
<header>
  <h1>__TITLE__</h1>
  <span class="meta">__SUB__</span>
  <input id="q" placeholder="filter nodes…" size="18">
  <select id="kind"><option value="">all kinds</option></select>
  <button id="reset">reset view</button>
  <span class="legend" id="legend"></span>
</header>
<div id="wrap"><canvas id="cv"></canvas><aside id="side">
  <h2>Nothing selected</h2>
  <p class="hint">Click a node to inspect it. Drag to pan, scroll to zoom,
     drag a node to pin it.</p>
</aside></div>
<script>
const DATA = __DATA__;
const cv=document.getElementById('cv'), ctx=cv.getContext('2d');
const side=document.getElementById('side');
let W=0,H=0,dpr=Math.min(devicePixelRatio||1,2);
function resize(){const r=cv.getBoundingClientRect();W=r.width;H=r.height;
  cv.width=W*dpr;cv.height=H*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);}
addEventListener('resize',()=>{resize();draw();});

const PALETTE=['#3b6ea5','#8a5a9c','#2f8f6b','#b4661e','#a34a58','#5c6f8a',
               '#7a8c3a','#9c6f3a','#4a7d99','#8c4a7d'];
const services=[...new Set(DATA.nodes.map(n=>n.service||n.kind))].sort();
const colorOf=s=>PALETTE[Math.max(0,services.indexOf(s))%PALETTE.length];

const N=DATA.nodes.map((n,i)=>({...n,
  x:W/2+Math.cos(i*2.4)*(120+i%180), y:H/2+Math.sin(i*2.4)*(120+i%180),
  vx:0,vy:0,r:4+Math.min(11,(n.rank||0)*260)}));
const byId=new Map(N.map(n=>[n.id,n]));
const L=DATA.edges.map(e=>({s:byId.get(e.src),t:byId.get(e.dst),
  kind:e.kind,prov:e.prov})).filter(e=>e.s&&e.t);

// degree, so hubs settle in the middle
N.forEach(n=>n.deg=0);
L.forEach(e=>{e.s.deg++;e.t.deg++;});

let tx=0,ty=0,scale=1,sel=null,drag=null,pan=null,alpha=1;
function step(){
  if(alpha<0.002) return;
  const k=alpha;
  for(let i=0;i<N.length;i++){const a=N[i];
    for(let j=i+1;j<N.length;j++){const b=N[j];
      let dx=b.x-a.x, dy=b.y-a.y, d2=dx*dx+dy*dy;
      if(d2<1) {d2=1;dx=Math.random()-0.5;dy=Math.random()-0.5;}
      if(d2>90000) continue;
      const f=1400/d2, d=Math.sqrt(d2), fx=dx/d*f, fy=dy/d*f;
      a.vx-=fx*k;a.vy-=fy*k;b.vx+=fx*k;b.vy+=fy*k;}}
  for(const e of L){
    const dx=e.t.x-e.s.x, dy=e.t.y-e.s.y, d=Math.hypot(dx,dy)||1;
    const f=(d-90)*0.012*k, fx=dx/d*f, fy=dy/d*f;
    e.s.vx+=fx;e.s.vy+=fy;e.t.vx-=fx;e.t.vy-=fy;}
  for(const n of N){
    if(n.pin){n.vx=n.vy=0;continue;}
    n.vx+=(W/2-n.x)*0.0016*k; n.vy+=(H/2-n.y)*0.0016*k;
    n.vx*=0.86;n.vy*=0.86; n.x+=n.vx;n.y+=n.vy;}
  alpha*=0.994;
}
const filtered=()=>{
  const q=document.getElementById('q').value.toLowerCase();
  const k=document.getElementById('kind').value;
  return n=>(!q||((n.name||'')+' '+(n.file||'')).toLowerCase().includes(q))
          &&(!k||n.kind===k);
};
function draw(){
  ctx.clearRect(0,0,W,H);
  ctx.save();ctx.translate(tx,ty);ctx.scale(scale,scale);
  const vis=filtered();
  ctx.lineWidth=1;
  for(const e of L){
    if(!vis(e.s)&&!vis(e.t)) continue;
    ctx.strokeStyle=e.prov==='INFERRED'?'rgba(140,140,140,.30)':'rgba(120,140,170,.55)';
    ctx.setLineDash(e.kind==='event'?[4,3]:[]);
    ctx.beginPath();ctx.moveTo(e.s.x,e.s.y);ctx.lineTo(e.t.x,e.t.y);ctx.stroke();
  }
  ctx.setLineDash([]);
  for(const n of N){
    const on=vis(n);
    ctx.globalAlpha=on?1:0.12;
    ctx.beginPath();ctx.arc(n.x,n.y,n.r,0,6.284);
    ctx.fillStyle=colorOf(n.service||n.kind);ctx.fill();
    if(sel&&sel.id===n.id){ctx.lineWidth=2.5;ctx.strokeStyle='#e0a030';ctx.stroke();ctx.lineWidth=1;}
    if(on&&(n.r>6||scale>1.5)){
      ctx.globalAlpha=on?0.9:0.1;
      ctx.fillStyle=getComputedStyle(document.body).color;
      ctx.font='11px ui-sans-serif';
      ctx.fillText((n.name||'').slice(0,26),n.x+n.r+3,n.y+3);
    }
  }
  ctx.globalAlpha=1;ctx.restore();
}
function loop(){step();draw();requestAnimationFrame(loop);}
const at=(mx,my)=>{const x=(mx-tx)/scale,y=(my-ty)/scale;
  let best=null,bd=1e9;
  for(const n of N){const d=Math.hypot(n.x-x,n.y-y);if(d<n.r+6&&d<bd){bd=d;best=n;}}
  return best;};
cv.addEventListener('mousedown',ev=>{const r=cv.getBoundingClientRect();
  const n=at(ev.clientX-r.left,ev.clientY-r.top);
  if(n){drag=n;n.pin=true;select(n);}else{pan={x:ev.clientX-tx,y:ev.clientY-ty};}});
addEventListener('mousemove',ev=>{const r=cv.getBoundingClientRect();
  if(drag){drag.x=(ev.clientX-r.left-tx)/scale;drag.y=(ev.clientY-r.top-ty)/scale;alpha=Math.max(alpha,.25);}
  else if(pan){tx=ev.clientX-pan.x;ty=ev.clientY-pan.y;draw();}});
addEventListener('mouseup',()=>{drag=null;pan=null;});
cv.addEventListener('wheel',ev=>{ev.preventDefault();
  const r=cv.getBoundingClientRect(),mx=ev.clientX-r.left,my=ev.clientY-r.top;
  const f=ev.deltaY<0?1.12:1/1.12, ns=Math.min(6,Math.max(.15,scale*f));
  tx=mx-(mx-tx)*(ns/scale); ty=my-(my-ty)*(ns/scale); scale=ns; draw();},{passive:false});
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function select(n){
  sel=n;
  const ins=L.filter(e=>e.t.id===n.id), outs=L.filter(e=>e.s.id===n.id);
  let h='<h2>'+esc(n.name||n.id)+'</h2>';
  h+='<div class="row"><span class="k">kind</span> '+esc(n.kind)+'</div>';
  if(n.service)h+='<div class="row"><span class="k">service</span> '+esc(n.service)+'</div>';
  if(n.file)h+='<div class="row"><span class="k">location</span> <code>'+esc(n.file)+(n.line?':'+n.line:'')+'</code></div>';
  if(n.rank)h+='<div class="row"><span class="k">centrality</span> '+n.rank.toFixed(5)+'</div>';
  h+='<div class="row"><span class="k">depended on by</span> '+ins.length+
     ' &nbsp; <span class="k">depends on</span> '+outs.length+'</div>';
  const list=(t,arr,pick)=>{if(!arr.length)return '';
    let s='<h2 style="margin-top:12px">'+t+' ('+arr.length+')</h2>';
    for(const e of arr.slice(0,40)){const o=pick(e);
      s+='<div class="row">'+esc(e.kind)+(e.prov==='INFERRED'?' <span class="k">?</span>':'')+
         ' &rarr; <code>'+esc(o.name||o.id)+'</code>'+
         (o.file?'<br><span class="k">'+esc(o.file)+'</span>':'')+'</div>';}
    return s;};
  h+=list('Inbound',ins,e=>e.s);
  h+=list('Outbound',outs,e=>e.t);
  h+='<p class="hint" style="margin-top:12px">? marks an INFERRED edge — a lead to verify, not a fact.</p>';
  side.innerHTML=h;
}
const kinds=[...new Set(N.map(n=>n.kind))].sort();
const ksel=document.getElementById('kind');
kinds.forEach(k=>{const o=document.createElement('option');o.value=k;o.textContent=k;ksel.appendChild(o);});
document.getElementById('legend').innerHTML=services.slice(0,10).map(s=>
  '<span><i class="dot" style="background:'+colorOf(s)+'"></i>'+esc(s)+'</span>').join('');
document.getElementById('q').addEventListener('input',draw);
ksel.addEventListener('change',draw);
document.getElementById('reset').addEventListener('click',()=>{
  tx=0;ty=0;scale=1;alpha=1;N.forEach(n=>n.pin=false);draw();});
resize();loop();
</script></body></html>
"""


def build(store, title="Codebase graph", max_nodes=1200):
    """Emit the interactive page. Ranked nodes win when we must truncate."""
    ranks = {r["id"]: r["rank"] for r in store.conn.execute("SELECT id, rank FROM ranks")}
    rows = list(store.conn.execute(
        "SELECT id, kind, name, container, file, line, repo, service FROM nodes "
        "WHERE kind != 'library'"))
    rows.sort(key=lambda r: -ranks.get(r["id"], 0.0))
    keep = rows[:max_nodes]
    kept = {r["id"] for r in keep}
    nodes = []
    for r in keep:
        nodes.append({"id": r["id"], "kind": r["kind"],
                      "name": r["name"] or (r["file"] or "").split("/")[-1],
                      "file": r["file"], "line": r["line"],
                      "service": r["service"] or r["repo"],
                      "rank": round(ranks.get(r["id"], 0.0), 6)})
    edges = []
    for e in store.conn.execute(
            "SELECT src, dst, kind, provenance FROM edges WHERE kind != 'uses-library'"):
        if e["src"] in kept and e["dst"] in kept:
            edges.append({"src": e["src"], "dst": e["dst"], "kind": e["kind"],
                          "prov": e["provenance"]})
    c = store.counts()
    sub = "%d nodes · %d edges · %s" % (
        len(nodes), len(edges), ", ".join(c["repos"][:6]) or "no repos")
    if len(rows) > max_nodes:
        sub += " · showing top %d of %d by centrality" % (max_nodes, len(rows))
    payload = json.dumps({"nodes": nodes, "edges": edges}, separators=(",", ":"))
    return (TEMPLATE.replace("__DATA__", payload)
                    .replace("__TITLE__", title)
                    .replace("__SUB__", sub))

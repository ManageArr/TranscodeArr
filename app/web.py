"""The web interface, as one self-contained page.

No build step, no framework, no CDN - a NAS container that needs npm to render
its own settings screen is a container that stops rendering its settings screen
the day a registry goes down. Plain HTML and a few hundred lines of vanilla JS,
served from memory.

The API token is held in localStorage and sent as a bearer header. A 401 clears
it and says so, because a token typed wrong used to leave a blank page with no
way back.
"""

PAGE = r"""<!doctype html><meta charset="utf-8"><title>TranscodeArr</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#0b1220;--panel:#111c30;--line:#1e293b;--ink:#cbd5e1;--dim:#94a3b8;--accent:#22d3ee;
       --ok:#34d399;--bad:#f87171;--warn:#fbbf24}
 *{box-sizing:border-box}
 body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--ink);margin:0;padding:0 1rem 3rem;
      max-width:70rem;margin-inline:auto;line-height:1.45}
 header{display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap;padding:1.2rem 0 .6rem}
 h1{color:var(--accent);font-size:1.25rem;margin:0;letter-spacing:.02em}
 #summary{color:var(--dim);font-size:.85rem}
 nav{display:flex;gap:.25rem;border-bottom:1px solid var(--line);margin-bottom:1.1rem;flex-wrap:wrap}
 nav button{background:none;border:0;border-bottom:2px solid transparent;color:var(--dim);cursor:pointer;
            padding:.55rem .85rem;font:inherit;font-size:.9rem}
 nav button.on{color:var(--accent);border-bottom-color:var(--accent)}
 section{display:none} section.on{display:block}
 table{width:100%;border-collapse:collapse;font-size:.85rem}
 td,th{border-bottom:1px solid var(--line);padding:.45rem .5rem;text-align:left;vertical-align:top}
 th{color:var(--dim);font-weight:500;font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
 .done{color:var(--ok)} .failed{color:var(--bad)} .running{color:var(--accent)}
 .queued,.cancelled{color:var(--dim)}
 code{color:var(--dim);font-size:.75rem;word-break:break-all}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:1rem;margin-bottom:1rem}
 .card h2{font-size:.95rem;margin:0 0 .2rem;color:var(--ink)}
 .card p.hint{color:var(--dim);font-size:.8rem;margin:.1rem 0 .9rem}
 label{display:block;margin-bottom:.9rem;font-size:.85rem}
 label .lab{display:flex;justify-content:space-between;align-items:center;gap:.5rem;margin-bottom:.25rem}
 label small{color:var(--dim);display:block;font-size:.76rem;font-weight:400;margin-top:.15rem}
 input[type=text],input[type=number],input[type=password],textarea,select{
   width:100%;background:#0a1122;border:1px solid var(--line);border-radius:6px;color:var(--ink);
   padding:.45rem .55rem;font:inherit;font-size:.85rem}
 textarea{min-height:4.5rem;resize:vertical}
 input[type=checkbox]{width:auto;transform:scale(1.15);margin-right:.4rem}
 button.act{background:var(--accent);color:#06121c;border:0;border-radius:6px;padding:.45rem .9rem;
            font:inherit;font-size:.85rem;font-weight:600;cursor:pointer}
 button.ghost{background:none;border:1px solid var(--line);color:var(--dim);border-radius:6px;
              padding:.35rem .7rem;font:inherit;font-size:.8rem;cursor:pointer}
 button.danger{border-color:#7f1d1d;color:var(--bad)}
 .row{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
 .tag{font-size:.68rem;text-transform:uppercase;letter-spacing:.05em;padding:.1rem .4rem;border-radius:4px;
      border:1px solid var(--line);color:var(--dim)}
 .tag.stored{color:var(--accent);border-color:#164e63} .tag.env{color:var(--warn);border-color:#78350f}
 .msg{padding:.5rem .7rem;border-radius:6px;font-size:.83rem;margin-bottom:.8rem;display:none}
 .msg.ok{display:block;background:#052e23;color:var(--ok)} .msg.err{display:block;background:#3f1414;color:var(--bad)}
 .browser{max-height:16rem;overflow:auto;border:1px solid var(--line);border-radius:6px}
 .browser div{padding:.3rem .55rem;cursor:pointer;font-size:.82rem;border-bottom:1px solid #16202f}
 .browser div:hover{background:#16233a;color:var(--accent)}
 .pill{display:inline-flex;align-items:center;gap:.4rem;background:#0a1122;border:1px solid var(--line);
       border-radius:999px;padding:.2rem .3rem .2rem .7rem;font-size:.8rem;margin:0 .4rem .4rem 0}
 .pill button{background:none;border:0;color:var(--dim);cursor:pointer;font-size:.95rem;line-height:1}
 .keyout{background:#052e23;border:1px solid #14532d;border-radius:6px;padding:.7rem;margin-bottom:.9rem;
         font-size:.82rem;display:none}
 .keyout code{color:var(--ok);font-size:.85rem;user-select:all}
 .now{background:linear-gradient(180deg,#12243c,#111c30);border:1px solid #164e63}
 .now .file{font-size:1rem;color:var(--ink);word-break:break-word;margin:.1rem 0 .1rem}
 .now .where{color:var(--dim);font-size:.78rem;word-break:break-all;margin-bottom:.7rem}
 .bar{height:8px;background:#0a1122;border-radius:999px;overflow:hidden;border:1px solid var(--line)}
 .bar i{display:block;height:100%;background:linear-gradient(90deg,#0891b2,var(--accent));transition:width .8s linear}
 .facts{display:flex;gap:1.4rem;flex-wrap:wrap;color:var(--dim);font-size:.8rem;margin-top:.6rem}
 .facts b{color:var(--ink);font-weight:600}
 .idle{color:var(--dim);font-size:.9rem;padding:.4rem 0}
 td.pos{color:var(--dim);width:2.5rem;text-align:right;font-variant-numeric:tabular-nums}
 tr.next td{color:var(--accent)}
</style>
<header><h1>TranscodeArr</h1><div id="summary">connecting...</div></header>
<nav>
 <button data-tab="queue" class="on">Queue</button>
 <button data-tab="jobs">History</button>
 <button data-tab="locations">Locations</button>
 <button data-tab="rules">Rules</button>
 <button data-tab="connections">Connections</button>
 <button data-tab="keys">API Keys</button>
</nav>

<section id="queue" class="on">
 <div class="card now" id="nowcard"><h2>Converting now</h2><div id="now"></div></div>
 <div class="card">
  <h2>Up next</h2>
  <p class="hint" id="queuehint">In the order they will run - one at a time, oldest first.</p>
  <table id="queuetable"></table>
 </div>
</section>

<section id="jobs">
 <div class="row" style="margin-bottom:.8rem">
  <select id="statefilter" style="width:auto">
   <option value="">All states</option><option value="queued">Queued</option>
   <option value="running">Running</option><option value="done">Done</option>
   <option value="failed">Failed</option><option value="cancelled">Cancelled</option>
  </select>
  <button class="ghost" onclick="refreshJobs()">Refresh</button>
 </div>
 <table id="jobtable"></table>
</section>

<section id="locations">
 <div class="msg" id="locmsg"></div>
 <div class="card">
  <h2>Watched folders</h2>
  <p class="hint">Scanned for dot-hidden files to convert. With none set, every media root is watched.</p>
  <div id="watchpills"></div>
  <div class="row"><input type="text" id="watchadd" placeholder="/media/TV">
   <button class="act" onclick="addWatch()">Add</button></div>
  <p class="hint" style="margin-top:.8rem">Browse the paths this container can actually see:</p>
  <div class="row"><code id="browsepath">/</code><button class="ghost" onclick="browse(upOne())">Up</button></div>
  <div class="browser" id="browser"></div>
 </div>
 <div class="card"><h2>Scanning</h2><div id="locform"></div>
  <button class="act" onclick="saveSettings('locform','locmsg')">Save</button></div>
</section>

<section id="rules">
 <div class="msg" id="rulemsg"></div>
 <div class="card"><h2>Conversion rules</h2>
  <p class="hint">Applied to every job from now on. Files already converted are unaffected.</p>
  <div id="ruleform"></div>
  <button class="act" onclick="saveSettings('ruleform','rulemsg')">Save</button></div>
 <div class="card"><h2>Housekeeping</h2><div id="genform"></div>
  <button class="act" onclick="saveSettings('genform','genmsg')">Save</button>
  <div class="msg" id="genmsg"></div></div>
</section>

<section id="connections">
 <div class="msg" id="arrmsg"></div>
 <div class="card">
  <h2>Radarr / Sonarr</h2>
  <p class="hint">After a file is replaced, the arr that owns it is asked to re-read it - otherwise the whole
   stack keeps reporting the old file's codec and runtime. Path mapping matters: this container and the arr
   mount the same folder at different places.</p>
  <table id="arrtable"></table>
 </div>
 <div class="card"><h2 id="arrformtitle">Add a connection</h2><div id="arrform"></div>
  <div class="row"><button class="act" onclick="saveArr()">Save</button>
   <button class="ghost" onclick="testArr()">Test</button>
   <button class="ghost" onclick="resetArrForm()">Clear</button></div></div>
</section>

<section id="keys">
 <div class="msg" id="keymsg"></div>
 <div class="keyout" id="keyout"></div>
 <div class="card"><h2>API keys</h2>
  <p class="hint">A key is shown once, when it is created, and only its hash is stored. The token set in the
   container's environment keeps working as a bootstrap key and cannot be revoked from here.</p>
  <div class="row"><input type="text" id="keyname" placeholder="e.g. ManageArr">
   <button class="act" onclick="mintKey()">Create key</button></div>
  <table id="keytable" style="margin-top:.9rem"></table>
 </div>
</section>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
const token=()=>localStorage.token||(localStorage.token=prompt('API token')||'');
let SPECS=[],SET={},SRC={},editingArr=null,timer=null;

function locked(msg){
 // A mistyped token used to sit in localStorage forever while the page stayed
 // blank and nothing said why. Forget it, say so, stop polling.
 delete localStorage.token; if(timer) clearInterval(timer); timer=null;
 $('summary').textContent=msg;
}
async function api(path,opts={}){
 const r=await fetch(path,{...opts,headers:{'Content-Type':'application/json',
   Authorization:'Bearer '+token(),...(opts.headers||{})}});
 if(r.status===401){locked('Wrong API token - reload the page to enter it again.');throw new Error('unauthorised');}
 const body=r.status===204?null:await r.json().catch(()=>null);
 if(!r.ok) throw new Error((body&&body.error)||('HTTP '+r.status));
 return body;
}
function say(el,text,bad){const n=$(el);n.className='msg '+(bad?'err':'ok');n.textContent=text;
 setTimeout(()=>{n.className='msg';},4000);}

document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));
 document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id===b.dataset.tab));
 // Refresh on arrival rather than showing whatever the last poll left behind.
 if(b.dataset.tab==='queue') refreshQueue().catch(()=>{});
 if(b.dataset.tab==='jobs') refreshJobs().catch(()=>{});
});

// ---- the queue: what is running, and what is next in running order -------
const fileOf=p=>String(p||'').split('/').pop();
const folderOf=p=>String(p||'').split('/').slice(0,-1).join('/');
function dur(s){
 if(s==null||!isFinite(s)) return '';
 s=Math.max(0,Math.round(s));
 const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
 if(d) return `${d}d ${h}h`;
 if(h) return `${h}h ${m}m`;
 if(m) return `${m}m ${s%60}s`;
 return `${s}s`;
}
async function refreshQueue(){
 const q=await api('/queue?limit=200');
 const r=(q.running||[])[0];
 if(r){
  const pct=Math.max(0,Math.min(100,r.progress||0));
  const elapsed=r.started?(Date.now()/1000-r.started):null;
  // Remaining is extrapolated from THIS file's own progress, not the fleet
  // average - a 20-minute episode behind a 2-hour film would otherwise read
  // as an hour out.
  const left=(pct>2&&elapsed)?elapsed*(100-pct)/pct:null;
  $('now').innerHTML=
   `<div class="file">${esc(fileOf(r.path))}</div>
    <div class="where">${esc(folderOf(r.path))}</div>
    <div class="bar"><i style="width:${pct}%"></i></div>
    <div class="facts"><span><b>${pct}%</b></span>
     <span>encoder <b>${esc(r.encoder||'?')}</b></span>
     ${elapsed?`<span>running <b>${dur(elapsed)}</b></span>`:''}
     ${left?`<span>about <b>${dur(left)}</b> left</span>`:''}
     <span><button class="ghost" data-cancel="${esc(r.id)}">Cancel</button></span></div>`;
 } else {
  $('now').innerHTML='<div class="idle">Nothing converting right now.</div>';
 }

 const rows=q.queued||[];
 $('queuetable').innerHTML='<tr><th class="pos">#</th><th>File</th><th>Folder</th><th>Type</th><th></th></tr>'+
  (rows.length?rows.map((x,i)=>
    `<tr class="${i===0?'next':''}"><td class="pos">${i+1}</td><td>${esc(fileOf(x.path))}</td>`+
    `<td><code>${esc(folderOf(x.path))}</code></td>`+
    `<td>${x.kind==='reveal'?'<span class="tag">reveal only</span>':'transcode'}</td>`+
    `<td><button class="ghost" data-cancel="${esc(x.id)}">Cancel</button></td></tr>`).join('')
   :'<tr><td colspan="5">Queue is empty.</td></tr>');

 const shown=rows.length,total=q.queued_total||0;
 let hint=total?`${total} waiting${shown<total?` (showing the next ${shown})`:''} - one at a time, oldest first.`
              :'Nothing waiting.';
 if(q.seconds_per_job&&total)
  hint+=` About ${dur(q.seconds_per_job)} each lately, so roughly ${dur(q.eta_seconds)} to clear.`;
 $('queuehint').textContent=hint;
}
// Every action is delegated off a data attribute holding only an id, so no
// server-supplied text - a file name, an arr name, or an error message an arr
// itself chose - is ever interpolated into a JavaScript string literal or an
// inline handler. esc() escapes HTML, not JS string context, and the two are
// not the same escape.
document.addEventListener('click',e=>{
 const d=e.target&&e.target.dataset;
 if(!d) return;
 if(d.cancel) cancelJob(d.cancel);
 if(d.editarr){const a=ARRS.find(x=>x.id===d.editarr); if(a) editArr(a);}
 if(d.delarr){const a=ARRS.find(x=>x.id===d.delarr); if(a) deleteArr(a.id,a.name);}
 if(d.revoke){const t=KEYS.find(x=>x.id===d.revoke); if(t) revokeKey(t.id,t.name);}
 if(d.browse!==undefined) browse(d.browse);
 if(d.addwatch) addBrowsed();
 if(d.dropwatch!==undefined) dropWatch(Number(d.dropwatch));
});

// ---- status + history ----------------------------------------------------
async function refreshHealth(){
 const z=await (await fetch('/healthz')).json();
 $('summary').innerHTML=`encoder <b style="color:${z.encoder==='libx264'?'var(--warn)':'var(--ok)'}">`+
  `${esc(z.encoder)}</b> &middot; ${z.queued} queued &middot; ${z.running} running &middot; v${esc(z.version)}`+
  (z.encoder==='libx264'?` <code>(${esc(z.encoder_reason)})</code>`:'');
}
async function refreshJobs(){
 const st=$('statefilter').value;
 const j=await api('/jobs?limit=60'+(st?'&state='+st:''));
 $('jobtable').innerHTML='<tr><th>State</th><th>File</th><th>%</th><th>Size</th><th>Result</th><th></th></tr>'+
  (j.jobs.length?j.jobs.map(x=>{
   const saved=(x.src_bytes&&x.out_bytes)?Math.round((1-x.out_bytes/x.src_bytes)*100)+'% smaller':'';
   const stop=(x.state==='queued'||x.state==='running')
     ?`<button class="ghost" onclick="cancelJob('${x.id}')">Cancel</button>`:'';
   return `<tr><td class="${x.state}">${esc(x.state)}${x.kind==='reveal'?' <span class="tag">reveal</span>':''}</td>`+
    `<td>${esc(x.path.split('/').pop())}</td><td>${x.progress??''}</td><td>${esc(saved)}</td>`+
    `<td><code>${esc(x.error||x.warning||x.rescan||x.output||'')}</code></td><td>${stop}</td></tr>`;
  }).join(''):'<tr><td colspan="6">Nothing here yet.</td></tr>');
}
async function cancelJob(id){
 try{await api('/jobs/'+id,{method:'DELETE'});await refreshQueue();await refreshJobs();}
 catch(e){alert(e.message);}
}

// ---- settings ------------------------------------------------------------
function field(spec){
 const v=SET[spec.key],src=SRC[spec.key];
 const tag=`<span class="tag ${src}">${src==='stored'?'saved here':src==='env'?'from container':'default'}</span>`;
 let input;
 if(spec.kind==='bool') input=`<input type="checkbox" data-key="${spec.key}" ${v?'checked':''}> ${esc(spec.label)}`;
 else if(spec.kind==='int') input=`<input type="number" data-key="${spec.key}" value="${esc(v)}">`;
 else if(spec.kind==='exts'||spec.kind==='patterns')
   input=`<textarea data-key="${spec.key}" placeholder="one per line">${esc((v||[]).join('\n'))}</textarea>`;
 else input=`<input type="text" data-key="${spec.key}" value="${esc(v)}">`;
 return `<label><span class="lab"><span>${spec.kind==='bool'?'':esc(spec.label)}</span>${tag}</span>`+
  `${input}<small>${esc(spec.help)}</small></label>`;
}
function renderSettings(){
 for(const [box,group] of [['locform','Locations'],['ruleform','Rules'],['genform','General']]){
  $(box).innerHTML=SPECS.filter(s=>s.group===group&&s.key!=='watch_roots').map(field).join('');
 }
 renderWatch();
}
async function loadSettings(){
 const d=await api('/api/settings');
 SPECS=d.specs;SET=d.values;SRC=d.sources;renderSettings();
}
async function saveSettings(box,msg){
 const updates={};
 $(box).querySelectorAll('[data-key]').forEach(el=>{
  updates[el.dataset.key]=el.type==='checkbox'?el.checked:el.value;
 });
 try{await api('/api/settings',{method:'PUT',body:JSON.stringify(updates)});await loadSettings();
  say(msg,'Saved. Takes effect on the next scan - no restart needed.');}
 catch(e){say(msg,e.message,true);}
}
function renderWatch(){
 const roots=SET.watch_roots||[];
 $('watchpills').innerHTML=roots.length?roots.map((p,i)=>
   `<span class="pill">${esc(p)}<button data-dropwatch="${i}" title="Remove">&times;</button></span>`).join('')
  :'<p class="hint">None set - every media root is watched.</p>';
}
async function putWatch(roots){
 try{await api('/api/settings',{method:'PUT',body:JSON.stringify({watch_roots:roots})});
  await loadSettings();say('locmsg','Saved.');}catch(e){say('locmsg',e.message,true);}
}
function addWatch(){const v=$('watchadd').value.trim();if(!v)return;
 $('watchadd').value='';putWatch([...(SET.watch_roots||[]),v]);}
function dropWatch(i){const r=[...(SET.watch_roots||[])];r.splice(i,1);putWatch(r);}

// ---- folder browser ------------------------------------------------------
let CURRENT='/';
function upOne(){return CURRENT.replace(/\/[^/]+\/?$/,'')||'/';}
async function browse(path){
 try{
  const d=await api('/api/fs?path='+encodeURIComponent(path||''));
  CURRENT=d.path;$('browsepath').textContent=d.path;
  $('browser').innerHTML=(d.entries.length?d.entries:[]).map(e=>
    `<div data-browse="${esc(e.path)}">${esc(e.name)}/</div>`).join('')
   +`<div data-addwatch="1" style="color:var(--accent)">+ watch ${esc(d.path)}</div>`;
 }catch(e){say('locmsg',e.message,true);}
}
function addBrowsed(){putWatch([...new Set([...(SET.watch_roots||[]),CURRENT])]);}

// ---- arr connections -----------------------------------------------------
function arrFormHtml(a){
 a=a||{name:'',kind:'radarr',base_url:'',arr_path:'',worker_path:'',enabled:true};
 return `<label><span class="lab">Name</span><input type="text" id="a_name" value="${esc(a.name)}"></label>
 <label><span class="lab">Kind</span><select id="a_kind">
  <option value="radarr" ${a.kind==='radarr'?'selected':''}>Radarr (movies)</option>
  <option value="sonarr" ${a.kind==='sonarr'?'selected':''}>Sonarr (series)</option></select></label>
 <label><span class="lab">Base URL</span><input type="text" id="a_url" value="${esc(a.base_url)}"
  placeholder="http://radarr:7878"></label>
 <label><span class="lab">API key</span><input type="password" id="a_key" placeholder="${a.id?'unchanged':''}">
  <small>Settings &rarr; General &rarr; API Key. Stored in this container's database; leave blank when editing to keep it.</small></label>
 <label><span class="lab">Library path as the arr sees it</span><input type="text" id="a_arrpath"
  value="${esc(a.arr_path)}" placeholder="/tv"><small>Its root folder, e.g. /tv or /movies.</small></label>
 <label><span class="lab">The same folder as this container sees it</span><input type="text" id="a_workerpath"
  value="${esc(a.worker_path)}" placeholder="/media/TV"><small>Both mount the same directory; without this pair a
  finished file cannot be matched to a title.</small></label>
 <label><input type="checkbox" id="a_enabled" ${a.enabled?'checked':''}> Enabled</label>`;
}
function resetArrForm(){editingArr=null;$('arrformtitle').textContent='Add a connection';$('arrform').innerHTML=arrFormHtml(null);}
function arrBody(){return{name:$('a_name').value,kind:$('a_kind').value,base_url:$('a_url').value,
 api_key:$('a_key').value,arr_path:$('a_arrpath').value,worker_path:$('a_workerpath').value,
 enabled:$('a_enabled').checked};}
let ARRS=[];
async function loadArrs(){
 const d=await api('/api/arrs');
 ARRS=d.arrs;
 $('arrtable').innerHTML='<tr><th>Name</th><th>Kind</th><th>URL</th><th>Path mapping</th><th>Last result</th><th></th></tr>'+
  (d.arrs.length?d.arrs.map(a=>`<tr><td>${esc(a.name)}${a.enabled?'':' <span class="tag">off</span>'}</td>
   <td>${esc(a.kind)}</td><td><code>${esc(a.base_url)}</code></td>
   <td><code>${esc(a.arr_path||'?')} &rarr; ${esc(a.worker_path||'?')}</code></td>
   <td><code class="${a.last_error?'failed':''}">${esc(a.last_error||'ok')}</code></td>
   <td><button class="ghost" data-editarr="${esc(a.id)}">Edit</button>
    <button class="ghost danger" data-delarr="${esc(a.id)}">Delete</button></td></tr>`).join('')
   :'<tr><td colspan="6">No connections yet.</td></tr>');
}
function editArr(json){const a=typeof json==='string'?JSON.parse(json):json;editingArr=a.id;
 $('arrformtitle').textContent='Edit '+a.name;$('arrform').innerHTML=arrFormHtml(a);
 document.querySelector('nav button[data-tab=connections]').click();window.scrollTo(0,document.body.scrollHeight);}
async function saveArr(){
 try{await api('/api/arrs'+(editingArr?'/'+editingArr:''),
   {method:editingArr?'PUT':'POST',body:JSON.stringify(arrBody())});
  resetArrForm();await loadArrs();say('arrmsg','Saved.');}catch(e){say('arrmsg',e.message,true);}
}
async function testArr(){
 try{const r=await api('/api/arrs/test',{method:'POST',body:JSON.stringify({...arrBody(),id:editingArr})});
  say('arrmsg',r.ok?'Connected - '+r.detail:'Failed: '+r.detail,!r.ok);}catch(e){say('arrmsg',e.message,true);}
}
async function deleteArr(id,name){
 if(!confirm('Delete the connection "'+name+'"? Nothing on the arr itself is changed.'))return;
 try{await api('/api/arrs/'+id,{method:'DELETE'});await loadArrs();say('arrmsg','Deleted.');}
 catch(e){say('arrmsg',e.message,true);}
}

// ---- API keys ------------------------------------------------------------
async function loadKeys(){
 const d=await api('/api/tokens');
 $('keytable').innerHTML='<tr><th>Name</th><th>Key</th><th>Created</th><th>Last used</th><th></th></tr>'+
  (d.tokens.length?d.tokens.map(t=>`<tr><td>${esc(t.name)}</td><td><code>${esc(t.prefix)}...</code></td>
   <td>${new Date(t.created*1000).toLocaleDateString()}</td>
   <td>${t.last_used?new Date(t.last_used*1000).toLocaleString():'never'}</td>
   <td><button class="ghost danger" data-revoke="${esc(t.id)}">Revoke</button></td></tr>`).join('')
   :'<tr><td colspan="5">No keys minted. The container environment token is in use.</td></tr>');
 KEYS=d.tokens;
}
let KEYS=[];
async function mintKey(){
 const name=$('keyname').value.trim()||'unnamed';
 try{const r=await api('/api/tokens',{method:'POST',body:JSON.stringify({name})});
  $('keyname').value='';
  $('keyout').style.display='block';
  $('keyout').innerHTML=`<b>${esc(name)}</b> created. Copy it now - it is not shown again:<br>
   <code>${esc(r.token)}</code>`;
  await loadKeys();}catch(e){say('keymsg',e.message,true);}
}
async function revokeKey(id,name){
 if(!confirm('Revoke "'+name+'"? Anything using it stops working immediately.'))return;
 try{await api('/api/tokens/'+id,{method:'DELETE'});await loadKeys();say('keymsg','Revoked.');}
 catch(e){say('keymsg',e.message,true);}
}

// ---- boot ----------------------------------------------------------------
async function tick(){
 try{
  await refreshHealth();
  // Only refresh the visible tab: polling the history and the queue together
  // every few seconds is two queries nobody is looking at.
  if($('queue').classList.contains('on')) await refreshQueue();
  else if($('jobs').classList.contains('on')) await refreshJobs();
 }catch(e){}
}
(async function(){
 resetArrForm();
 try{await refreshHealth();await loadSettings();await loadArrs();await loadKeys();
  await refreshQueue();await refreshJobs();
  browse((SET.watch_roots||[])[0]||'');
  timer=setInterval(tick,4000);
 }catch(e){if(String(e.message)!=='unauthorised')$('summary').textContent=e.message;}
})();
$('statefilter').onchange=refreshJobs;
</script>"""

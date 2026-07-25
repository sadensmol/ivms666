const $ = id => document.getElementById(id);
let devices = [];
const chans = {};      // deviceId -> [channels]
let timer = null;

function setStatus(msg, isErr) { const s=$('status'); s.textContent=msg; s.className='status'+(isErr?' err':''); }
function escapeHtml(s){ return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function jfetch(url, opts){ const r=await fetch(url,opts); if(!r.ok) throw new Error(await r.text()); return r.json(); }

/* ---- device CRUD ---- */
async function loadDevices() {
  try {
    devices = await jfetch('/devices');
    renderDevices();
    if (!devices.length) { setStatus('No cameras yet — click "Add camera".'); return; }
    setStatus('Loaded ' + devices.length + ' device(s).');
    devices.forEach(loadChannels);
    syncAuto();
  } catch (e) { setStatus('Error loading devices: ' + e.message, true); }
}

let devEditId = null;
function openAddDevice() {
  devEditId = null;
  $('devTitle').textContent = 'Add camera';
  $('dName').value=''; $('dHost').value=''; $('dPort').value='80';
  $('dUser').value=''; $('dPass').value=''; $('dPass').placeholder=''; $('dRtsp').value='554';
  $('devMsg').textContent=''; $('devOverlay').classList.add('open');
}
function openEditDevice(d) {
  devEditId = d.id;
  $('devTitle').textContent = 'Edit camera';
  $('dName').value=d.name||''; $('dHost').value=d.host; $('dPort').value=d.port;
  $('dUser').value=d.user||''; $('dPass').value=''; $('dRtsp').value=d.rtspPort||'554';
  $('dPass').placeholder = d.hasPassword ? 'leave blank to keep' : '';
  $('devMsg').textContent=''; $('devOverlay').classList.add('open');
}
async function saveDevice() {
  const body = { name:$('dName').value.trim(), host:$('dHost').value.trim(),
                 port:$('dPort').value.trim(), user:$('dUser').value,
                 rtsp_port:$('dRtsp').value.trim() };
  const pass = $('dPass').value;
  if (pass !== '' || devEditId === null) body.password = pass;
  if (!body.host || !body.port) { $('devMsg').textContent='Host and port are required.'; return; }
  try {
    if (devEditId === null) await jfetch('/devices', {method:'POST', body:JSON.stringify(body)});
    else await jfetch('/devices/'+encodeURIComponent(devEditId), {method:'PUT', body:JSON.stringify(body)});
    $('devOverlay').classList.remove('open');
    await loadDevices();
  } catch (e) { $('devMsg').textContent = 'Error: ' + e.message; }
}
async function deleteDevice(d) {
  if (!confirm('Remove "' + (d.name||d.host) + '" and its saved credentials?')) return;
  try { await fetch('/devices/'+encodeURIComponent(d.id), {method:'DELETE'}); await loadDevices(); }
  catch (e) { setStatus('Error: ' + e.message, true); }
}

/* ---- rendering ---- */
function renderDevices() {
  const root = $('devices'); root.innerHTML='';
  if (!devices.length) {
    root.innerHTML = '<div class="empty">No cameras configured yet.<br>Click <b>+ Add camera</b> to get started.</div>';
    return;
  }
  for (const d of devices) {
    const sec = document.createElement('section'); sec.className='device';
    const hiddenN = (d.hidden||[]).length;
    sec.innerHTML =
      '<div class="dhead"><span class="dot" data-dot="'+d.id+'"></span>' +
      '<b>'+escapeHtml(d.name||d.host)+'</b>' +
      '<small>'+escapeHtml(d.host)+':'+escapeHtml(d.port)+(d.user?('  ·  '+escapeHtml(d.user)):'')+'</small>' +
      '<span class="grow"></span>' +
      '<button class="iconbtn" data-reset="'+d.id+'">'+(hiddenN?('Reset hidden ('+hiddenN+')'):'Reset hidden')+'</button>' +
      '<button class="iconbtn" data-edit="'+d.id+'">Edit</button>' +
      '<button class="iconbtn danger" data-del="'+d.id+'">Delete</button></div>' +
      '<div class="grid" data-grid="'+d.id+'"></div>';
    root.appendChild(sec);
  }
  root.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>openEditDevice(devices.find(x=>x.id===b.dataset.edit)));
  root.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>deleteDevice(devices.find(x=>x.id===b.dataset.del)));
  root.querySelectorAll('[data-reset]').forEach(b=>b.onclick=()=>resetHidden(b.dataset.reset));
  // re-render tiles for devices already discovered
  for (const d of devices) if (chans[d.id]) buildTiles(d);
}

function visibleChannels(d) {
  const hidden = new Set(d.hidden||[]);
  return (chans[d.id]||[]).filter(c => !hidden.has(c.id));
}

async function loadChannels(d) {
  const dot = document.querySelector('[data-dot="'+d.id+'"]');
  try {
    chans[d.id] = await jfetch('/channels?device='+encodeURIComponent(d.id));
    if (dot) dot.className = 'dot ' + (chans[d.id].length ? 'ok' : 'err');
    buildTiles(d);
    visibleChannels(d).forEach(ch => captureTile(d, ch));
  } catch (e) {
    if (dot) dot.className = 'dot err';
    const g = document.querySelector('[data-grid="'+d.id+'"]');
    if (g) g.innerHTML = '<div class="empty">Could not reach device: '+escapeHtml(e.message)+'</div>';
  }
}

function buildTiles(d) {
  const grid = document.querySelector('[data-grid="'+d.id+'"]');
  if (!grid) return;
  grid.innerHTML='';
  const vis = visibleChannels(d);
  if (!vis.length) { grid.innerHTML='<div class="empty">No visible cameras.</div>'; }
  for (const ch of vis) {
    const tile=document.createElement('div'); tile.className='tile';
    tile.innerHTML =
      '<div class="bar"><b>'+escapeHtml(ch.name)+'</b><small>id '+escapeHtml(ch.id)+'</small><span class="grow"></span>' +
      '<button class="iconbtn" data-live="'+ch.id+'">Live</button>' +
      '<button class="iconbtn" data-motion="'+ch.id+'">Motion</button>' +
      '<button class="iconbtn" data-dl="'+ch.id+'">Save</button>' +
      '<button class="iconbtn danger" data-remove="'+ch.id+'" title="Remove from view">✕</button></div>' +
      '<img data-img="'+ch.id+'" alt=""><div class="msg" data-msg="'+ch.id+'">—</div>';
    grid.appendChild(tile);
  }
  grid.querySelectorAll('[data-dl]').forEach(b=>b.onclick=()=>downloadTile(d,b.dataset.dl));
  grid.querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>removeTile(d,b.dataset.remove));
  grid.querySelectorAll('[data-motion]').forEach(b=>b.onclick=()=>openMotion(d,b.dataset.motion));
  grid.querySelectorAll('[data-live]').forEach(b=>b.onclick=()=>openLive(d,b.dataset.live));
}

function quality(){ return $('quality').value; }
function snapURL(deviceId, chId){
  return '/snapshot?device='+encodeURIComponent(deviceId)+'&ch='+encodeURIComponent(chId)+
         '&res='+encodeURIComponent(quality())+'&ts='+Date.now();
}

async function captureTile(d, ch) {
  const img=document.querySelector('[data-grid="'+d.id+'"] [data-img="'+ch.id+'"]');
  const msg=document.querySelector('[data-grid="'+d.id+'"] [data-msg="'+ch.id+'"]');
  if (!img) return;
  try {
    const r=await fetch(snapURL(d.id, ch.id));
    if (!r.ok) { msg.textContent='Error: '+await r.text(); msg.className='msg err'; return; }
    const blob=await r.blob();
    if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
    const url=URL.createObjectURL(blob); img.dataset.url=url; img.src=url; ch._blob=blob;
    msg.textContent=new Date().toLocaleTimeString()+'  ·  '+(blob.size/1024).toFixed(0)+' KB'; msg.className='msg';
  } catch (e) { msg.textContent='Error: '+e.message; msg.className='msg err'; }
}

function refreshAll(){ for (const d of devices) visibleChannels(d).forEach(ch=>captureTile(d,ch)); }

function downloadTile(d, id) {
  const ch=(chans[d.id]||[]).find(c=>c.id===id);
  if (!ch||!ch._blob) { setStatus('Nothing captured yet for '+id, true); return; }
  const a=document.createElement('a'); a.href=URL.createObjectURL(ch._blob);
  a.download='camera-'+(d.name||d.host)+'-'+id+'-'+new Date().toISOString().replace(/[:.]/g,'-')+'.jpg';
  a.click(); setTimeout(()=>URL.revokeObjectURL(a.href),5000);
}

async function persistHidden(d) {
  await jfetch('/devices/'+encodeURIComponent(d.id), {method:'PUT', body:JSON.stringify({hidden:d.hidden})});
}
async function removeTile(d, id) {
  d.hidden = Array.from(new Set([...(d.hidden||[]), id]));
  buildTiles(d); renderDevices();
  try { await persistHidden(d); } catch(e){ setStatus('Could not save view: '+e.message, true); }
}
async function resetHidden(deviceId) {
  const d = devices.find(x=>x.id===deviceId); if (!d) return;
  d.hidden = [];
  buildTiles(d); renderDevices(); visibleChannels(d).forEach(ch=>captureTile(d,ch));
  try { await persistHidden(d); } catch(e){ setStatus('Could not save view: '+e.message, true); }
}

function syncAuto() {
  if (timer) { clearInterval(timer); timer=null; }
  const secs=parseFloat($('interval').value);
  if ($('auto').checked && secs>0) timer=setInterval(refreshAll, secs*1000);
}

/* ---- live view (real-time RTSP -> MJPEG via ffmpeg) ---- */
const L = { d:null, ch:null, active:false, frames:0 };
async function openLive(d, id) {
  const ch=(chans[d.id]||[]).find(c=>c.id===id); if (!ch) return;
  L.d=d; L.ch=ch; L.active=true; L.frames=0;
  $('liveTitle').textContent='Live — '+(d.name||d.host)+' / '+ch.name;
  $('liveFps').textContent='…'; $('liveMsg').textContent='';
  $('liveOverlay').classList.add('open');
  startLiveStream();
}
async function startLiveStream() {
  if (!L.active) return;
  const img=$('liveImg'); img.removeAttribute('src');
  $('liveMsg').textContent='Checking RTSP connection…';
  try {
    const chk=await jfetch('/live/check?device='+encodeURIComponent(L.d.id));
    if (!chk.ok) { liveError(chk.message); return; }
  } catch (e) { liveError(e.message); return; }
  $('liveMsg').textContent='Starting stream…'; $('liveFps').textContent='…';
  // <img> renders the multipart MJPEG stream directly; onload fires per frame.
  // Live always uses the main stream (HD).
  img.onload=()=>{ L.frames++; $('liveMsg').textContent=''; };
  img.onerror=()=>{ if (L.active) liveError('stream stopped (RTSP unreachable, wrong port, or ffmpeg error)'); };
  img.src='/live?device='+encodeURIComponent(L.d.id)+'&ch='+encodeURIComponent(L.ch.id)+
          '&stream=main&ts='+Date.now();
}
function liveError(msg){ $('liveFps').textContent='offline'; $('liveMsg').className='hint'; $('liveMsg').textContent='⚠ '+msg; }
function saveLiveFrame(){
  const img=$('liveImg');
  if (!img.naturalWidth) { $('liveMsg').className='hint'; $('liveMsg').textContent='No frame yet.'; return; }
  const cv=document.createElement('canvas'); cv.width=img.naturalWidth; cv.height=img.naturalHeight;
  cv.getContext('2d').drawImage(img,0,0);   // same-origin stream -> canvas not tainted
  cv.toBlob(b=>{
    if(!b){ $('liveMsg').textContent='Could not capture frame.'; return; }
    const a=document.createElement('a'); a.href=URL.createObjectURL(b);
    const name=(L.d&&(L.d.name||L.d.host))||'camera';
    a.download='live-'+name+'-'+(L.ch?L.ch.id:'')+'-'+new Date().toISOString().replace(/[:.]/g,'-')+'.jpg';
    a.click(); setTimeout(()=>URL.revokeObjectURL(a.href),5000);
    $('liveMsg').className='hint'; $('liveMsg').textContent='Saved frame.';
  }, 'image/jpeg', 0.95);
}
function closeLive() {
  L.active=false; $('liveOverlay').classList.remove('open');
  const img=$('liveImg'); img.onload=img.onerror=null; img.removeAttribute('src'); // stops ffmpeg server-side
  L.d=L.ch=null;
}
setInterval(()=>{ if (L.active) { $('liveFps').textContent=L.frames+' fps'; L.frames=0; } }, 1000);

/* ---- motion editor ---- */
const M = { d:null, ch:null, cols:0, rows:0, cells:[], painting:false, paintVal:1 };
async function openMotion(d, id) {
  const ch=(chans[d.id]||[]).find(c=>c.id===id); if (!ch) return;
  M.d=d; M.ch=ch;
  $('mtitle').textContent='Motion detection — '+(d.name||d.host)+' / '+ch.name;
  setMMsg('Loading current settings…');
  $('overlay').classList.add('open');
  $('mimg').src=snapURL(d.id, ch.id);
  try {
    const data=await jfetch('/motion?device='+encodeURIComponent(d.id)+'&input='+encodeURIComponent(ch.input));
    if (data.format!=='grid') { setMMsg('This camera does not expose a grid motion area (format: '+data.format+').','err'); M.cols=0; drawGrid(); return; }
    M.cols=data.cols; M.rows=data.rows; M.cells=data.cells.map(r=>r.slice());
    $('msens').value=data.sensitivity; $('msensval').textContent=data.sensitivity;
    setMMsg('Loaded. Green cells are the current detection area.','ok');
    sizeCanvas(); drawGrid();
  } catch (e) { setMMsg('Error: '+e.message,'err'); }
}
function closeMotion(){ $('overlay').classList.remove('open'); M.d=M.ch=null; }
function setMMsg(msg,cls){ const e=$('mmsg'); e.textContent=msg; e.className='mmsg '+(cls||''); }
function sizeCanvas(){ const img=$('mimg'),cv=$('mcanvas'); cv.width=img.clientWidth; cv.height=img.clientHeight; }
function drawGrid(){
  const cv=$('mcanvas'),ctx=cv.getContext('2d'); ctx.clearRect(0,0,cv.width,cv.height);
  if (!M.cols||!M.rows) return;
  const cw=cv.width/M.cols, chh=cv.height/M.rows;
  for (let r=0;r<M.rows;r++) for (let c=0;c<M.cols;c++)
    if (M.cells[r][c]) { ctx.fillStyle='rgba(46,160,67,.45)'; ctx.fillRect(c*cw,r*chh,cw,chh); }
  ctx.strokeStyle='rgba(255,255,255,.14)'; ctx.lineWidth=1;
  for (let c=0;c<=M.cols;c++){ ctx.beginPath(); ctx.moveTo(c*cw,0); ctx.lineTo(c*cw,cv.height); ctx.stroke(); }
  for (let r=0;r<=M.rows;r++){ ctx.beginPath(); ctx.moveTo(0,r*chh); ctx.lineTo(cv.width,r*chh); ctx.stroke(); }
}
function cellAt(ev){
  const cv=$('mcanvas'),rect=cv.getBoundingClientRect();
  const c=Math.max(0,Math.min(M.cols-1,Math.floor((ev.clientX-rect.left)/rect.width*M.cols)));
  const r=Math.max(0,Math.min(M.rows-1,Math.floor((ev.clientY-rect.top)/rect.height*M.rows)));
  return {r,c};
}
function onDown(ev){ if(!M.cols)return; ev.preventDefault(); const{r,c}=cellAt(ev); M.painting=true; M.paintVal=M.cells[r][c]?0:1; M.cells[r][c]=M.paintVal; drawGrid(); }
function onMove(ev){ if(!M.painting)return; const{r,c}=cellAt(ev); if(M.cells[r][c]!==M.paintVal){ M.cells[r][c]=M.paintVal; drawGrid(); } }
function onUp(){ M.painting=false; }
async function saveMotion(){
  if (!M.d||!M.cols) return;
  setMMsg('Saving to device…');
  try {
    const data=await jfetch('/motion',{method:'POST',body:JSON.stringify({device:M.d.id,input:M.ch.input,cells:M.cells,sensitivity:parseInt($('msens').value,10)})});
    if (!data.ok) { setMMsg('Failed: '+(data.message||'unknown'),'err'); return; }
    setMMsg('Saved to device.','ok');
  } catch (e) { setMMsg('Error: '+e.message,'err'); }
}

$('addBtn').onclick=openAddDevice;
$('devClose').onclick=()=>$('devOverlay').classList.remove('open');
$('devSave').onclick=saveDevice;
$('refresh').onclick=refreshAll;
$('auto').onchange=syncAuto; $('interval').onchange=syncAuto;
$('liveClose').onclick=closeLive;
$('liveSave').onclick=saveLiveFrame;
(function(){ const q=localStorage.getItem('camquality'); if(q) $('quality').value=q; })();
$('quality').onchange=()=>{ localStorage.setItem('camquality',$('quality').value); refreshAll(); };
$('mclose').onclick=closeMotion; $('msave').onclick=saveMotion;
$('mselall').onclick=()=>{ M.cells=M.cells.map(r=>r.map(()=>1)); drawGrid(); };
$('mclear').onclick=()=>{ M.cells=M.cells.map(r=>r.map(()=>0)); drawGrid(); };
$('msens').oninput=()=>{ $('msensval').textContent=$('msens').value; };
$('mimg').onload=()=>{ sizeCanvas(); drawGrid(); };
$('mcanvas').addEventListener('pointerdown',onDown);
$('mcanvas').addEventListener('pointermove',onMove);
window.addEventListener('pointerup',onUp);
window.addEventListener('resize',()=>{ if($('overlay').classList.contains('open')){ sizeCanvas(); drawGrid(); } });
setInterval(()=>{ $('clock').textContent=new Date().toLocaleTimeString(); },1000);
loadDevices();

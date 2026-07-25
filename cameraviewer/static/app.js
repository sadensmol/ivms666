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
    startMotionPolling();
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
async function rebootDevice(d) {
  if (!d) return;
  if (!confirm('Reboot "'+(d.name||d.host)+'"? All its cameras go offline for ~1 minute.')) return;
  setStatus('Rebooting '+(d.name||d.host)+'…');
  try {
    const r=await jfetch('/reboot',{method:'POST',body:JSON.stringify({device:d.id})});
    if (!r.ok) { setStatus('Reboot failed: '+(r.message||'unknown'), true); return; }
    setStatus('Reboot command sent to '+(d.name||d.host)+'. It will be offline briefly.');
  } catch (e) { setStatus('Reboot failed: '+e.message, true); }
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
      '<button class="iconbtn" data-diag="'+d.id+'">Diagnose</button>' +
      '<button class="iconbtn" data-reboot="'+d.id+'">Reboot</button>' +
      '<button class="iconbtn" data-edit="'+d.id+'">Edit</button>' +
      '<button class="iconbtn danger" data-del="'+d.id+'">Delete</button></div>' +
      '<div class="grid" data-grid="'+d.id+'"></div>';
    root.appendChild(sec);
  }
  root.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>openEditDevice(devices.find(x=>x.id===b.dataset.edit)));
  root.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>deleteDevice(devices.find(x=>x.id===b.dataset.del)));
  root.querySelectorAll('[data-reset]').forEach(b=>b.onclick=()=>resetHidden(b.dataset.reset));
  root.querySelectorAll('[data-reboot]').forEach(b=>b.onclick=()=>rebootDevice(devices.find(x=>x.id===b.dataset.reboot)));
  root.querySelectorAll('[data-diag]').forEach(b=>b.onclick=()=>openDiagnose(devices.find(x=>x.id===b.dataset.diag)));
  // re-render tiles for devices already discovered
  for (const d of devices) if (chans[d.id]) buildTiles(d);
}

/* ---- per-tile settings menu (⚙) ---- */
function closeTileMenus() {
  document.querySelectorAll('.menu.open').forEach(m=>m.classList.remove('open'));
}
function toggleTileMenu(ev, grid, chId) {
  ev.stopPropagation();
  const menu = grid.querySelector('[data-menu="'+chId+'"]');
  const wasOpen = menu.classList.contains('open');
  closeTileMenus();
  if (!wasOpen) menu.classList.add('open');
}
document.addEventListener('click', closeTileMenus);  // click anywhere else closes it

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
    const tile=document.createElement('div'); tile.className='tile'; tile.dataset.tile=ch.id;
    tile.innerHTML =
      '<div class="bar"><b>'+escapeHtml(ch.name)+'</b><small>id '+escapeHtml(ch.id)+'</small><span class="grow"></span>' +
      '<button class="iconbtn" data-live="'+ch.id+'">Live</button>' +
      '<button class="iconbtn" data-dl="'+ch.id+'">Save</button>' +
      '<span class="tilemenu"><button class="iconbtn" data-settings="'+ch.id+'" title="Settings">⚙</button>' +
        '<div class="menu" data-menu="'+ch.id+'">' +
          '<button data-motion="'+ch.id+'">Motion detection area</button>' +
          '<button class="danger" data-remove="'+ch.id+'">Hide this camera</button>' +
        '</div></span></div>' +
      '<img data-img="'+ch.id+'" alt=""><div class="msg" data-msg="'+ch.id+'">—</div>' +
      '<div class="corners"></div>';
    grid.appendChild(tile);
  }
  grid.querySelectorAll('[data-dl]').forEach(b=>b.onclick=()=>saveTile(d,b.dataset.dl));
  grid.querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>{ closeTileMenus(); removeTile(d,b.dataset.remove); });
  grid.querySelectorAll('[data-settings]').forEach(b=>b.onclick=e=>toggleTileMenu(e,grid,b.dataset.settings));
  grid.querySelectorAll('[data-motion]').forEach(b=>b.onclick=()=>{ closeTileMenus(); openMotion(d,b.dataset.motion); });
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

async function saveTile(d, id) {
  const msg=document.querySelector('[data-grid="'+d.id+'"] [data-msg="'+id+'"]');
  if (msg) { msg.textContent='Saving…'; msg.className='msg'; }
  try {
    const r=await jfetch('/save',{method:'POST',body:JSON.stringify({device:d.id, ch:id})});
    if (!r.ok) { if(msg){ msg.textContent='Save failed: '+(r.message||'unknown'); msg.className='msg err'; } return; }
    if (msg) { msg.textContent='Saved!'; msg.className='msg'; }
  } catch (e) { if(msg){ msg.textContent='Save failed: '+e.message; msg.className='msg err'; } }
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

/* ---- motion indicator (per-camera, from the device alert stream) ---- */
const motionState = {};   // deviceId -> { inputIndex: bool }
let motionTimer = null, motionTileTimer = null;
function startMotionPolling() {
  if (motionTimer) clearInterval(motionTimer);
  motionTimer = setInterval(pollMotion, 1500);
  pollMotion();
  // tiles with active motion refresh every 1s; all other tiles stay on the
  // configured "Refresh (s)" interval (syncAuto).
  if (motionTileTimer) clearInterval(motionTileTimer);
  motionTileTimer = setInterval(refreshMotionTiles, 1000);
}
function refreshMotionTiles() {
  for (const d of devices) {
    const st = motionState[d.id] || {};
    for (const ch of visibleChannels(d)) if (st[String(ch.input)]) captureTile(d, ch);
  }
}
async function pollMotion() {
  for (const d of devices) {
    try {
      const s = await jfetch('/motion/state?device='+encodeURIComponent(d.id));
      applyMotion(d, s.channels || {});
    } catch (e) { /* transient — keep the last known state */ }
  }
}
function applyMotion(d, channels) {
  const prev = motionState[d.id] || {};
  motionState[d.id] = channels;
  for (const ch of (chans[d.id]||[])) {
    const active = !!channels[String(ch.input)];
    const tile = document.querySelector('[data-grid="'+d.id+'"] .tile[data-tile="'+ch.id+'"]');
    if (tile) tile.classList.toggle('motion', active);
    // fresh inactive->active: refresh the visible frame + pop the captured shot
    if (active && !prev[String(ch.input)]) { captureTile(d, ch); showMotionPopup(d, ch); }
  }
  // auto-close the popup once ITS channel's motion has ended (no more active state)
  if (motCur && motCur.deviceId === d.id && !channels[motCur.input]) closeMotionPopup();
}

/* ---- motion popup: full-screen, live-refreshing while motion lasts ---- */
let motTimer = null;   // 1s image-refresh interval
let motCur = null;     // {deviceId, chId, input, name, dname, host} currently shown
function showMotionPopup(d, ch) {
  // Only the Live view genuinely conflicts (it's already showing real-time video);
  // over everything else the motion alert should still pop.
  if ($('liveOverlay').classList.contains('open')) return;
  motCur = { deviceId:d.id, chId:ch.id, input:String(ch.input), name:ch.name, dname:d.name, host:d.host };
  refreshMotionImg();
  $('motOverlay').classList.add('open');
  if (motTimer) clearInterval(motTimer);
  motTimer = setInterval(refreshMotionImg, 1000);   // live-update the big image every 1s
}
function refreshMotionImg() {
  if (!motCur) return;
  $('motTitle').textContent = 'Motion — '+(motCur.dname||motCur.host)+' / '+motCur.name+'  ·  '+new Date().toLocaleTimeString();
  // request the max-resolution still (not the view quality), cache-busted each tick
  $('motImg').src = '/snapshot?device='+encodeURIComponent(motCur.deviceId)+'&ch='+encodeURIComponent(motCur.chId)+
                    '&res=1280x720&ts='+Date.now();
}
function closeMotionPopup() {
  if (motTimer) { clearInterval(motTimer); motTimer=null; }
  motCur = null;
  $('motOverlay').classList.remove('open'); $('motImg').removeAttribute('src');
}

/* ---- diagnose (motion -> email -> UI pipeline) ---- */
let diagDev = null;
async function openDiagnose(d) {
  if (!d) return;
  diagDev = d;
  $('diagTitle').textContent = 'Diagnose — '+(d.name||d.host);
  $('diagFix').style.display = 'none';
  $('diagBody').innerHTML = '<div class="diagsum">Checking '+escapeHtml(d.name||d.host)+'…</div>';
  $('diagOverlay').classList.add('open');
  try {
    const rep = await jfetch('/diagnose?device='+encodeURIComponent(d.id));
    renderDiag(rep);
  } catch (e) { $('diagBody').innerHTML = '<div class="chk bad">Error: '+escapeHtml(e.message)+'</div>'; }
}
function chk(ok, label) {
  return '<div class="chk '+(ok?'ok':'bad')+'"><span class="ico">'+(ok?'✓':'✗')+'</span><span>'+escapeHtml(label)+'</span></div>';
}
function renderDiag(rep) {
  if (rep.error) { $('diagBody').innerHTML = '<div class="chk bad">Error: '+escapeHtml(rep.error)+'</div>'; return; }
  const issues = [].concat(...rep.channels.map(c=>c.issues||[]));
  const problems = issues.length || !rep.smtp.ok;
  let html = '<div class="diagsum">'+(problems
    ? '⚠ '+issues.length+' issue(s) found.'
    : '✓ All good — motion will e-mail and show in the app.')+'</div>';
  // SMTP (device-wide)
  html += '<div class="diagch"><h4>E-mail (SMTP)</h4>' +
    chk(rep.smtp.ok, rep.smtp.ok
      ? 'SMTP configured — '+rep.smtp.receivers+' recipient(s)'
      : (rep.smtp.issue || 'SMTP not configured (set server + recipient on the DVR Email page)')) + '</div>';
  for (const c of rep.channels) {
    html += '<div class="diagch"><h4>'+escapeHtml(c.name)+' <small style="color:#9aa3af">id '+escapeHtml(c.id)+'</small></h4>';
    if (!c.reachable) { html += chk(false, 'Not reachable / disabled input ('+escapeHtml(c.detail||'')+')') + '</div>'; continue; }
    html += chk(c.motion_enabled, 'Motion detection enabled');
    html += chk(c.area_painted, 'Detection area painted');
    html += chk(c.email_linked, 'E-mail on motion (email linkage)');
    html += chk(c.center_linked, 'Shows in app (Notify Surveillance Center)');
    for (const i of (c.issues||[])) if (!i.fixable) html += '<div class="chk bad"><span class="ico">→</span><span>'+escapeHtml(i.msg)+'</span></div>';
    html += '</div>';
  }
  $('diagBody').innerHTML = html;
  $('diagFix').style.display = rep.fixable ? '' : 'none';
}
async function fixDiag() {
  if (!diagDev) return;
  $('diagFix').disabled = true; $('diagFix').textContent = 'Fixing…';
  try {
    const r = await jfetch('/diagnose/fix', {method:'POST', body:JSON.stringify({device:diagDev.id})});
    if (!r.ok) { setStatus('Fix failed: '+(r.message||'unknown'), true); }
    else {
      const applied = (r.fixes||[]).map(f=>f.name+': +'+f.added.join(', ')).join('  ·  ');
      setStatus(applied ? ('Fixed — '+applied) : 'Nothing to fix.');
      renderDiag(r.report);
      if (r.fixes && r.fixes.length) $('diagBody').insertAdjacentHTML('afterbegin',
        '<div class="chk ok"><span class="ico">✓</span><span>Applied: '+escapeHtml(applied)+'</span></div>');
    }
  } catch (e) { setStatus('Fix failed: '+e.message, true); }
  finally { $('diagFix').disabled = false; $('diagFix').textContent = 'Fix issues'; }
}

/* ---- settings (image save path) ---- */
async function openSettings() {
  $('setMsg').textContent=''; $('setMsg').className='mmsg';
  try { const s=await jfetch('/settings'); $('setPath').value=s.save_path||''; }
  catch (e) { $('setMsg').textContent='Error: '+e.message; $('setMsg').className='mmsg err'; }
  $('setOverlay').classList.add('open');
}
async function saveSettings() {
  try {
    const s=await jfetch('/settings',{method:'PUT',body:JSON.stringify({save_path:$('setPath').value.trim()})});
    $('setPath').value=s.save_path; $('setMsg').textContent='Saved: '+s.save_path; $('setMsg').className='mmsg ok';
  } catch (e) { $('setMsg').textContent='Error: '+e.message; $('setMsg').className='mmsg err'; }
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
$('settingsBtn').onclick=openSettings;
$('setClose').onclick=()=>$('setOverlay').classList.remove('open');
$('setSave').onclick=saveSettings;
$('diagClose').onclick=()=>$('diagOverlay').classList.remove('open');
$('diagFix').onclick=fixDiag;
$('devClose').onclick=()=>$('devOverlay').classList.remove('open');
$('devSave').onclick=saveDevice;
$('refresh').onclick=refreshAll;
$('auto').onchange=syncAuto; $('interval').onchange=syncAuto;
$('liveClose').onclick=closeLive;
$('liveSave').onclick=saveLiveFrame;
$('motClose').onclick=closeMotionPopup;
$('motOverlay').onclick=e=>{ if(e.target===$('motOverlay')) closeMotionPopup(); };
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

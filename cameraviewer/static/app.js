const $ = id => document.getElementById(id);
let devices = [];
let groupList = [];    // all group names (incl. empty ones)
const chans = {};      // deviceId -> [channels]
let timer = null;

function setStatus(msg, isErr) { const s=$('status'); s.textContent=msg; s.className='status'+(isErr?' err':''); }
function escapeHtml(s){ return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function jfetch(url, opts){ const r=await fetch(url,opts); if(!r.ok) throw new Error(await r.text()); return r.json(); }

/* ---- device CRUD ---- */
async function loadDevices() {
  try {
    [devices, groupList] = await Promise.all([jfetch('/devices'), jfetch('/groups').catch(()=>[])]);
    renderDevices();
    if (!devices.length && !groupList.length) { setStatus('No cameras yet — click "Add DVR" or "Add RTSP stream".'); return; }
    setStatus('Loaded ' + devices.length + ' device(s)' + (groupList.length?(' · '+groupList.length+' group(s)'):'') + '.');
    devices.forEach(loadChannels);
    syncAuto();
    startMotionPolling();
  } catch (e) { setStatus('Error loading devices: ' + e.message, true); }
}
function fillGroupDatalist() {
  $('groupDatalist').innerHTML = groupList.map(g=>'<option value="'+escapeHtml(g)+'">').join('');
}
// Create-group uses an INLINE input, not the native prompt() — some browsers block
// prompt() (e.g. after "don't allow this page to create dialogs"), which made the
// button silently do nothing. The button reveals the input; Enter submits, Esc cancels.
function createGroup() {
  const inp = $('newGroupInput');
  $('addGroupBtn').hidden = true; inp.hidden = false; inp.value = ''; inp.focus();
}
function cancelGroup() {
  $('newGroupInput').hidden = true; $('addGroupBtn').hidden = false;
}
async function submitGroup(name) {
  name = (name||'').trim();
  cancelGroup();
  if (!name) return;
  try { groupList = await jfetch('/groups', {method:'POST', body:JSON.stringify({name})}); renderDevices(); }
  catch (e) { setStatus('Could not create group: '+e.message, true); }
}
async function deleteGroup(name) {
  const members = devices.filter(d => (d.group||'') === name);
  let url = '/groups/'+encodeURIComponent(name);
  if (members.length) {
    if (!confirm('Group "'+name+'" is not empty — it has '+members.length+' camera(s).\n\n'+
                 'Delete the group AND its '+members.length+' camera(s)? This cannot be undone.')) return;
    url += '?devices=1';    // cascade: delete member devices too
  } else if (!confirm('Delete empty group "'+name+'"?')) {
    return;
  }
  try {
    await fetch(url, {method:'DELETE'});
    members.forEach(d => dropDeviceFrames(d.id));   // release their cached frames
    await loadDevices();
  } catch (e) { setStatus('Could not delete group: '+e.message, true); }
}
async function toggleGroupActive(name) {
  const members = devices.filter(d=>(d.group||'')===name);
  const anyActive = members.some(d=>visibleChannels(d).some(c=>!isPaused(d,c.id)));
  // 1) flip state + paint the tiles synchronously (unpause + uncollapse) with NO grabs,
  //    so all camera tiles appear at once immediately (empty, "…"), like the DVR view.
  for (const d of members) {
    d.inactive = anyActive ? visibleChannels(d).map(c=>c.id) : [];
    for (const ch of visibleChannels(d)) applyTileState(d, ch.id, false);
    updateCollapse(d);
  }
  syncGroupToggle(name);                 // group body visible NOW (before any network)
  // 2) tiles are on screen — fetch frames; each fills in as its grab returns.
  for (const d of members) activeChannels(d).forEach(ch => showTile(d, ch));
  // 3) persist in the background — don't block the UI (or the un-collapse) on it.
  members.forEach(d => persistInactive(d).catch(()=>{}));
}

let devEditId = null;
let devMode = 'dvr';   // 'dvr' (add DVR), 'rtsp' (add RTSP — paste URL), 'edit'
function setDevMode(mode) {
  devMode = mode;
  const pasteUrl = mode==='rtsp';            // Add RTSP stream: paste one or more URLs
  $('devUrlField').style.display = pasteUrl ? '' : 'none';
  $('devFields').style.display = pasteUrl ? 'none' : '';
  $('devNameField').style.display = pasteUrl ? 'none' : '';   // name comes from the URL
  updatePathVisibility();
}
// Split the RTSP-URLs textarea into individual URLs — delimited by any mix of
// new lines, commas or spaces.
function parseUrls() {
  return ($('dUrls').value || '').split(/[\s,]+/).map(s=>s.trim()).filter(Boolean);
}
function updatePathVisibility() {            // RTSP path only matters when ISAPI is off
  $('devPathField').style.display = (devMode!=='rtsp' && !$('dIsapi').checked) ? '' : 'none';
}
function setFields(d) {   // fill the standard fields from a device (or blanks)
  d = d || {};
  $('dName').value=d.name||''; $('dHost').value=d.host||''; $('dPort').value=d.port||'80';
  $('dIsapi').checked = d.isapiEnabled!==false;
  $('dAgent').value=d.agentgreenPort||'8090'; $('dAgentEnabled').checked = !!d.agentgreenEnabled;
  $('dRtsp').value=d.rtspPort||'554';
  $('dUser').value=d.user||''; $('dPass').value=''; $('dPath').value=d.path||'';
  $('dPass').placeholder = d.hasPassword ? 'leave blank to keep' : '';
}
function openAddDevice(group) {
  devEditId = null; fillGroupDatalist();
  setDevMode('dvr'); setFields(null);
  $('devTitle').textContent = 'Add DVR';
  $('dGroup').value = group||'';
  $('devMsg').textContent=''; $('devOverlay').classList.add('open');
}
function openAddRtsp(group) {
  devEditId = null; fillGroupDatalist();
  setDevMode('rtsp'); setFields(null);
  $('devTitle').textContent = 'Add RTSP stream(s)';
  $('dUrls').value='';       // one empty multi-line box (paste many URLs)
  $('dGroup').value = group||'';
  $('devMsg').textContent=''; $('devOverlay').classList.add('open');
}
function openEditDevice(d) {
  devEditId = d.id; fillGroupDatalist();
  setDevMode('edit'); setFields(d);
  $('devTitle').textContent = d.kind==='rtsp' ? 'Edit RTSP stream' : 'Edit DVR';
  updatePathVisibility();
  $('dGroup').value = d.group||'';
  $('devMsg').textContent=''; $('devOverlay').classList.add('open');
}
async function saveDevice() {
  if (devMode === 'rtsp') {                    // Add RTSP: one or more URLs -> one stream each
    const urls = parseUrls();
    if (!urls.length) { $('devMsg').textContent='At least one RTSP URL is required.'; return; }
    const group = $('dGroup').value.trim();
    try {
      for (const url of urls) await jfetch('/devices', {method:'POST', body:JSON.stringify({rtsp_url:url, group})});
      $('devOverlay').classList.remove('open');
      await loadDevices();
    } catch (e) { $('devMsg').textContent = 'Error: ' + e.message; }
    return;
  }
  const isapi = $('dIsapi').checked;
  const body = { name:$('dName').value.trim(), host:$('dHost').value.trim(), port:$('dPort').value.trim(),
                 rtsp_port:$('dRtsp').value.trim(), agentgreen_port:$('dAgent').value.trim(),
                 agentgreen_enabled:$('dAgentEnabled').checked,
                 user:$('dUser').value, isapi_enabled:isapi, path:$('dPath').value.trim() };
  const pass = $('dPass').value;
  if (pass !== '' || devEditId === null) body.password = pass;
  if (!body.host) { $('devMsg').textContent='Host is required.'; return; }
  if (!isapi && !body.path) { $('devMsg').textContent='An RTSP path is required when ISAPI is off.'; return; }
  if (!isapi) body.audio_only = false;   // editing an RTSP stream re-probes (video may be back)
  body.group = $('dGroup').value.trim();      // '' -> ungrouped
  try {
    if (devEditId === null) await jfetch('/devices', {method:'POST', body:JSON.stringify(body)});
    else { await jfetch('/devices/'+encodeURIComponent(devEditId), {method:'PUT', body:JSON.stringify(body)}); dropDeviceFrames(devEditId); }
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
  try { await fetch('/devices/'+encodeURIComponent(d.id), {method:'DELETE'}); dropDeviceFrames(d.id); await loadDevices(); }
  catch (e) { setStatus('Error: ' + e.message, true); }
}

/* ---- rendering ---- */
function renderDevices() {
  const root = $('devices'); root.innerHTML='';
  if (!devices.length && !groupList.length) {
    root.innerHTML = '<div class="empty">No cameras yet.<br>Click <b>+ Add DVR</b> or <b>+ Add RTSP stream</b> to get started.</div>';
    return;
  }
  // an RTSP stream is a single camera, not a device -> synthesize its one channel
  for (const d of devices) if (d.kind==='rtsp' && !chans[d.id]) chans[d.id]=[{id:'rtsp',input:'1',name:d.name}];
  // a group's DVRs render as nested device sections; its RTSP streams render as
  // bare camera tiles in one shared grid (a line of cameras), no per-stream header.
  const gridOf = ds => ds.length ? '<div class="grid streamgrid">'+ds.map(streamTileHTML).join('')+'</div>' : '';
  const grouped = new Set();
  let html = '';
  for (const g of groupList) {
    const members = devices.filter(d => (d.group||'') === g);
    members.forEach(d => grouped.add(d.id));
    const streams = members.filter(d => d.kind==='rtsp');
    const dvrs = members.filter(d => d.kind!=='rtsp');
    html += '<div class="groupwrap"><div class="ghead">' +
      '<label class="gtoggle" title="All devices in this group active — uncheck to pause them"><input type="checkbox" data-gactive="'+escapeHtml(g)+'" checked></label>' +
      '<span class="gname">'+escapeHtml(g)+'</span><span class="gcount">('+members.length+')</span>' +
      '<span class="grow"></span>' +
      '<button class="iconbtn" data-gadddvr="'+escapeHtml(g)+'">+ DVR</button>' +
      '<button class="iconbtn" data-gaddrtsp="'+escapeHtml(g)+'">+ RTSP</button>' +
      '<button class="iconbtn danger" data-gdel="'+escapeHtml(g)+'">Delete group</button></div>' +
      '<div class="gmembers">' +
      gridOf(streams) + dvrs.map(deviceSectionHTML).join('') +
      (members.length ? '' : '<div class="gempty">Empty group — add a stream above, or set a device’s Group to “'+escapeHtml(g)+'” in its Edit dialog.</div>') +
      '</div></div>';
  }
  const ungrouped = devices.filter(d => !grouped.has(d.id));
  html += ungrouped.filter(d=>d.kind!=='rtsp').map(deviceSectionHTML).join('');   // DVRs
  html += gridOf(ungrouped.filter(d=>d.kind==='rtsp'));                            // loose streams
  root.innerHTML = html;

  root.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>openEditDevice(devices.find(x=>x.id===b.dataset.edit)));
  root.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>deleteDevice(devices.find(x=>x.id===b.dataset.del)));
  root.querySelectorAll('[data-reset]').forEach(b=>b.onclick=()=>resetHidden(b.dataset.reset));
  root.querySelectorAll('[data-reboot]').forEach(b=>b.onclick=()=>rebootDevice(devices.find(x=>x.id===b.dataset.reboot)));
  root.querySelectorAll('[data-diag]').forEach(b=>b.onclick=()=>openDiagnose(devices.find(x=>x.id===b.dataset.diag)));
  root.querySelectorAll('[data-devactive]').forEach(cb=>cb.onchange=()=>toggleDeviceActive(cb.dataset.devactive));
  root.querySelectorAll('[data-gdel]').forEach(b=>b.onclick=()=>deleteGroup(b.dataset.gdel));
  root.querySelectorAll('[data-gadddvr]').forEach(b=>b.onclick=()=>openAddDevice(b.dataset.gadddvr));
  root.querySelectorAll('[data-gaddrtsp]').forEach(b=>b.onclick=()=>openAddRtsp(b.dataset.gaddrtsp));
  root.querySelectorAll('[data-gactive]').forEach(cb=>cb.onchange=()=>toggleGroupActive(cb.dataset.gactive));
  wireStreamTiles(root);
  for (const d of devices) {                    // fill tiles: DVRs via buildTiles, streams inline
    if (d.kind!=='rtsp' && chans[d.id]) buildTiles(d);
    else if (d.kind==='rtsp') { activeChannels(d).forEach(ch=>showTile(d,ch)); if (d.group) syncGroupToggle(d.group); }
  }
}
// One RTSP stream = one bare camera tile (data-grid=device id so captureTile finds
// its img wherever it's laid out); Edit/Delete live in its ⚙ menu (no device header).
function streamTileHTML(d) {
  const paused = isPaused(d, 'rtsp');
  const audio = !!d.audioOnly;                 // no video track -> audio-only tile
  const nm = d.name || ((d.host||'')+(d.path||'')) || 'stream';
  const body = audio
    ? '<div class="audioph" data-audio="rtsp">🔊 Audio only<small>no video track</small></div>'
    : '<img data-img="rtsp" alt="">';
  return '<div class="tile'+(paused?' paused':'')+'" data-grid="'+d.id+'" data-tile="rtsp">' +
    '<div class="bar">' +
      '<label class="actck" title="Active — uncheck to pause refresh"><input type="checkbox" data-sactive="'+d.id+'"'+(paused?'':' checked')+'></label>' +
      '<b title="'+escapeHtml(nm)+'">'+escapeHtml(nm)+'</b><span class="grow"></span>' +
      '<button class="iconbtn" data-slive="'+d.id+'">'+(audio?'Listen':'Live')+'</button>' +
      (audio ? '' : '<button class="iconbtn" data-ssave="'+d.id+'">Save</button>') +
      '<span class="tilemenu"><button class="iconbtn" data-ssettings="'+d.id+'" title="Settings">⚙</button>' +
        '<div class="menu" data-menu="s_'+d.id+'">' +
          '<button data-sedit="'+d.id+'">Edit stream</button>' +
          (d.group ? '<button data-sungroup="'+d.id+'">Remove from group</button>' : '') +
          '<button class="danger" data-sdel="'+d.id+'">Delete stream</button>' +
        '</div></span></div>' +
    body + '<div class="msg" data-msg="rtsp">'+(audio?'Audio only':(paused?'Paused':'—'))+'</div>' +
    '<div class="corners"></div></div>';
}
function wireStreamTiles(root) {
  const dev = id => devices.find(x=>x.id===id);
  root.querySelectorAll('[data-sactive]').forEach(cb=>cb.onchange=()=>toggleActive(dev(cb.dataset.sactive), 'rtsp'));
  root.querySelectorAll('[data-slive]').forEach(b=>b.onclick=()=>openLive(dev(b.dataset.slive), 'rtsp'));
  root.querySelectorAll('[data-ssave]').forEach(b=>b.onclick=()=>saveTile(dev(b.dataset.ssave), 'rtsp'));
  root.querySelectorAll('[data-ssettings]').forEach(b=>b.onclick=e=>toggleTileMenu(e, b.closest('.tilemenu'), 's_'+b.dataset.ssettings));
  root.querySelectorAll('[data-sedit]').forEach(b=>b.onclick=()=>{ closeTileMenus(); openEditDevice(dev(b.dataset.sedit)); });
  root.querySelectorAll('[data-sdel]').forEach(b=>b.onclick=()=>{ closeTileMenus(); deleteDevice(dev(b.dataset.sdel)); });
  root.querySelectorAll('[data-sungroup]').forEach(b=>b.onclick=()=>{ closeTileMenus(); moveToGroup(dev(b.dataset.sungroup), ''); });
}
async function moveToGroup(d, group) {
  if (!d) return;
  try { await jfetch('/devices/'+encodeURIComponent(d.id), {method:'PUT', body:JSON.stringify({group})}); await loadDevices(); }
  catch (e) { setStatus('Error: '+e.message, true); }
}
function deviceSectionHTML(d) {
  const hiddenN = (d.hidden||[]).length;
  const isRtsp = d.kind === 'rtsp';
  const sub = isRtsp ? escapeHtml(d.host)+':'+escapeHtml(d.rtspPort)+escapeHtml(d.path||'')
                     : escapeHtml(d.host)+':'+escapeHtml(d.port)+(d.user?('  ·  '+escapeHtml(d.user)):'');
  return '<section class="device"><div class="dhead">' +
    '<label class="devtoggle" title="All cameras active — uncheck to pause this device"><input type="checkbox" data-devactive="'+d.id+'" checked></label>' +
    '<span class="dot" data-dot="'+d.id+'"></span>' +
    '<b>'+escapeHtml(d.name||d.host||'RTSP stream')+'</b>' +
    '<small>'+sub+(isRtsp?'  ·  RTSP':'')+'</small>' +
    '<span class="grow"></span>' +
    (isRtsp ? '' :
      '<button class="iconbtn" data-reset="'+d.id+'">'+(hiddenN?('Reset hidden ('+hiddenN+')'):'Reset hidden')+'</button>' +
      '<button class="iconbtn" data-diag="'+d.id+'">Diagnose</button>' +
      '<button class="iconbtn" data-reboot="'+d.id+'">Reboot</button>') +
    '<button class="iconbtn" data-edit="'+d.id+'">Edit</button>' +
    '<button class="iconbtn danger" data-del="'+d.id+'">Delete</button></div>' +
    '<div class="grid" data-grid="'+d.id+'"></div></section>';
}
// keep the device + group header checkboxes in sync with the actual per-tile state
function syncDeviceToggle(d) {
  const cb = document.querySelector('[data-devactive="'+d.id+'"]');
  if (cb && Array.isArray(chans[d.id])) cb.checked = visibleChannels(d).some(c=>!isPaused(d,c.id));
  if (d.group) syncGroupToggle(d.group);
}
function syncGroupToggle(g) {
  const members = devices.filter(d => (d.group||'') === g);
  const anyActive = members.some(d => Array.isArray(chans[d.id]) && visibleChannels(d).some(c=>!isPaused(d,c.id)));
  document.querySelectorAll('[data-gactive]').forEach(cb=>{
    if (cb.dataset.gactive !== g) return;
    cb.checked = anyActive;
    // group off (all members paused) -> fold its body, leaving just the header.
    const body = cb.closest('.groupwrap') && cb.closest('.groupwrap').querySelector('.gmembers');
    if (body) body.classList.toggle('collapsed', !anyActive);
  });
}
// The tile element for a channel — a stream tile carries data-grid itself; a DVR
// tile lives inside a grid[data-grid]. Handles both.
function tileEl(gridId, tileId) {
  return document.querySelector('[data-grid="'+gridId+'"][data-tile="'+tileId+'"]')
      || document.querySelector('[data-grid="'+gridId+'"] .tile[data-tile="'+tileId+'"]');
}
// Reflect one channel's paused/active state on its tile WITHOUT re-rendering the page
// (a full re-render would wipe every tile's <img> and re-grab them all — so pausing
// camera A must not touch camera B). Grabs a fresh frame when it becomes active,
// unless `capture` is false (caller wants the empty tile shown first, then fills it).
function applyTileState(d, chId, capture=true) {
  const tile = tileEl(d.id, chId);
  if (!tile) return;
  const paused = isPaused(d, chId);
  tile.classList.toggle('paused', paused);
  const cb = tile.querySelector('.actck input');
  if (cb) cb.checked = !paused;
  const msg = tile.querySelector('[data-msg="'+chId+'"]');
  if (paused) { if (msg) { msg.textContent='Paused'; msg.className='msg'; } }
  else {
    if (msg && msg.textContent==='Paused') { msg.textContent='…'; msg.className='msg'; }
    const ch=(chans[d.id]||[]).find(c=>c.id===chId); if (capture && ch) captureTile(d, ch);
  }
}
function updateCollapse(d) {   // hide a DVR grid when all its cameras are paused (header only)
  if (d.kind==='rtsp') return;
  const grid = document.querySelector('.grid[data-grid="'+d.id+'"]');
  if (grid) grid.classList.toggle('collapsed', !visibleChannels(d).some(c=>!isPaused(d,c.id)));
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
// Active = visible AND not paused. Paused (inactive) tiles are excluded from every
// refresh loop and show no frame, to save DVR/RTSP bandwidth.
function activeChannels(d) {
  const off = new Set(d.inactive||[]);
  return visibleChannels(d).filter(c => !off.has(c.id));
}
function isPaused(d, id) { return (d.inactive||[]).includes(id); }

async function loadChannels(d) {
  if (d.kind === 'rtsp') {   // one synthetic channel; its bare tile is already rendered
    chans[d.id] = [{id:'rtsp', input:'1', name:d.name||((d.host||'')+(d.path||''))}];
    activeChannels(d).forEach(ch => showTile(d, ch));
    if (d.group) syncGroupToggle(d.group);
    return;
  }
  const dot = document.querySelector('[data-dot="'+d.id+'"]');
  try {
    chans[d.id] = await jfetch('/channels?device='+encodeURIComponent(d.id));
    if (dot) dot.className = 'dot ' + (chans[d.id].length ? 'ok' : 'err');
    buildTiles(d);
    activeChannels(d).forEach(ch => showTile(d, ch));
  } catch (e) {
    if (dot) dot.className = 'dot err';
    const g = document.querySelector('[data-grid="'+d.id+'"]');
    if (g) g.innerHTML = '<div class="empty">Could not reach device: '+escapeHtml(e.message)+'</div>';
  }
}

function buildTiles(d) {
  const grid = document.querySelector('[data-grid="'+d.id+'"]');
  if (!grid) return;
  syncDeviceToggle(d);
  grid.innerHTML='';
  const vis = visibleChannels(d);
  if (!vis.length) { grid.innerHTML='<div class="empty">No visible cameras.</div>'; return; }
  // all tiles paused -> hide the grid (header only), but KEEP the tiles in the DOM so a
  // per-tile toggle can re-show one without a full re-render (re-enable via the ▸ header toggle)
  grid.classList.toggle('collapsed', !vis.some(c=>!isPaused(d,c.id)));
  const isRtsp = d.kind === 'rtsp';
  for (const ch of vis) {
    const paused = isPaused(d, ch.id);
    const tile=document.createElement('div'); tile.className='tile'+(paused?' paused':''); tile.dataset.tile=ch.id;
    tile.innerHTML =
      '<div class="bar">' +
      '<label class="actck" title="Active — uncheck to pause refresh"><input type="checkbox" data-active="'+ch.id+'"'+(paused?'':' checked')+'></label>' +
      '<b title="'+escapeHtml(ch.name)+'">'+escapeHtml(ch.name)+'</b><small>id '+escapeHtml(ch.id)+'</small><span class="grow"></span>' +
      '<button class="iconbtn" data-live="'+ch.id+'">Live</button>' +
      '<button class="iconbtn" data-dl="'+ch.id+'">Save</button>' +
      '<span class="tilemenu"><button class="iconbtn" data-settings="'+ch.id+'" title="Settings">⚙</button>' +
        '<div class="menu" data-menu="'+ch.id+'">' +
          (isRtsp ? '' :   // motion + event log need ISAPI (DVR only)
            '<button data-motion="'+ch.id+'">Motion detection area</button>' +
            '<button data-events="'+ch.id+'">Event log</button>') +
          '<button class="danger" data-remove="'+ch.id+'">Hide this camera</button>' +
        '</div></span></div>' +
      '<img data-img="'+ch.id+'" alt=""><div class="msg" data-msg="'+ch.id+'">'+(paused?'Paused':'—')+'</div>' +
      '<div class="corners"></div>';
    grid.appendChild(tile);
  }
  grid.querySelectorAll('[data-active]').forEach(cb=>cb.onchange=()=>toggleActive(d,cb.dataset.active));
  grid.querySelectorAll('[data-dl]').forEach(b=>b.onclick=()=>saveTile(d,b.dataset.dl));
  grid.querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>{ closeTileMenus(); removeTile(d,b.dataset.remove); });
  grid.querySelectorAll('[data-settings]').forEach(b=>b.onclick=e=>toggleTileMenu(e,grid,b.dataset.settings));
  grid.querySelectorAll('[data-motion]').forEach(b=>b.onclick=()=>{ closeTileMenus(); openMotion(d,b.dataset.motion); });
  grid.querySelectorAll('[data-events]').forEach(b=>b.onclick=()=>{ closeTileMenus(); openEvents(d,b.dataset.events); });
  grid.querySelectorAll('[data-live]').forEach(b=>b.onclick=()=>openLive(d,b.dataset.live));
}

function quality(){ return $('quality').value; }
function snapURL(deviceId, chId){
  return '/snapshot?device='+encodeURIComponent(deviceId)+'&ch='+encodeURIComponent(chId)+
         '&res='+encodeURIComponent(quality())+'&ts='+Date.now();
}

// Last successful frame per tile, keyed by device+channel id. Survives renderDevices()
// (which wipes the DOM) and loadChannels() (which rebuilds chans[]), so a CRUD op on
// ONE camera can restore every other tile's frame from cache instead of re-grabbing —
// no blank flash, no needless RTSP session (453). A pure perf cache: in-memory only,
// dropped on reload, never a data source.
const frameCache = new Map();
const frameKey = (dId, chId) => dId+'|'+chId;
// Forget a device's cached frames (on edit -> re-grab fresh from the new source; on
// delete -> release the blob URLs). Other devices' frames stay cached and untouched.
function dropDeviceFrames(dId) {
  const pfx = dId+'|';
  for (const k of [...frameCache.keys()]) if (k.startsWith(pfx)) {
    const f=frameCache.get(k); if (f&&f.url) URL.revokeObjectURL(f.url); frameCache.delete(k);
  }
}

// Flip a stream to audio-only mode: no video track, so stop grabbing frames, render
// the "🔊 Audio only" tile, and persist the flag (reverts if video returns later).
function markAudioOnly(d) {
  if (d.audioOnly) return;
  d.audioOnly = true;
  jfetch('/devices/'+encodeURIComponent(d.id), {method:'PUT', body:JSON.stringify({audio_only:true})}).catch(()=>{});
  const tile = tileEl(d.id, 'rtsp');
  if (tile) { tile.outerHTML = streamTileHTML(d); wireStreamTiles($('devices')); }
}
async function captureTile(d, ch) {
  if (d.audioOnly) return;             // audio-only stream — nothing to grab
  const img=document.querySelector('[data-grid="'+d.id+'"] [data-img="'+ch.id+'"]');
  const msg=document.querySelector('[data-grid="'+d.id+'"] [data-msg="'+ch.id+'"]');
  if (!img) return;
  try {
    const r=await fetch(snapURL(d.id, ch.id));
    if (!r.ok) { msg.textContent='Error: '+await r.text(); msg.className='msg err'; return; }
    if ((r.headers.get('content-type')||'').includes('application/json')) {
      const j=await r.json().catch(()=>({}));   // server signalled no video track
      if (j.audio_only) markAudioOnly(d);
      return;
    }
    const blob=await r.blob();
    if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
    const url=URL.createObjectURL(blob); img.dataset.url=url; img.src=url; ch._blob=blob;
    msg.textContent=new Date().toLocaleTimeString()+'  ·  '+(blob.size/1024).toFixed(0)+' KB'; msg.className='msg';
    frameCache.set(frameKey(d.id, ch.id), {url, text: msg.textContent, cls: msg.className});
  } catch (e) { msg.textContent='Error: '+e.message; msg.className='msg err'; }
}
// Paint a tile's cached frame instantly (no network). Returns true if a frame existed.
function restoreTile(d, ch) {
  const f = frameCache.get(frameKey(d.id, ch.id));
  if (!f) return false;
  const img=document.querySelector('[data-grid="'+d.id+'"] [data-img="'+ch.id+'"]');
  if (!img) return false;
  img.dataset.url=f.url; img.src=f.url;
  const msg=document.querySelector('[data-grid="'+d.id+'"] [data-msg="'+ch.id+'"]');
  if (msg) { msg.textContent=f.text; msg.className=f.cls; }
  return true;
}
// Initial fill after a (re-)render: restore from cache if we've shown this tile before
// (a CRUD op elsewhere must not re-grab it), else do the one live grab a new tile needs.
function showTile(d, ch) { if (!restoreTile(d, ch)) captureTile(d, ch); }

function refreshAll(){ for (const d of devices) activeChannels(d).forEach(ch=>captureTile(d,ch)); }

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
  buildTiles(d); renderDevices(); activeChannels(d).forEach(ch=>showTile(d,ch));
  try { await persistHidden(d); } catch(e){ setStatus('Could not save view: '+e.message, true); }
}

/* ---- active/inactive (pause a tile's refresh to save DVR/RTSP bandwidth) ---- */
async function persistInactive(d) {
  await jfetch('/devices/'+encodeURIComponent(d.id), {method:'PUT', body:JSON.stringify({inactive:d.inactive||[]})});
}
async function toggleActive(d, id) {
  const off = new Set(d.inactive||[]);
  if (off.has(id)) off.delete(id); else off.add(id);
  d.inactive = Array.from(off);
  applyTileState(d, id);           // update ONLY this tile (others untouched)
  updateCollapse(d); syncDeviceToggle(d);
  try { await persistInactive(d); } catch(e){ setStatus('Could not save: '+e.message, true); }
}
async function toggleDeviceActive(deviceId) {
  const d = devices.find(x=>x.id===deviceId); if (!d) return;
  const vis = visibleChannels(d);
  const anyActive = vis.some(c => !isPaused(d, c.id));
  d.inactive = anyActive ? vis.map(c=>c.id) : [];   // pause all, or re-activate all
  for (const ch of vis) applyTileState(d, ch.id);   // per-tile update (captures re-activated)
  updateCollapse(d); syncDeviceToggle(d);
  try { await persistInactive(d); } catch(e){ setStatus('Could not save: '+e.message, true); }
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
    for (const ch of activeChannels(d)) if (st[String(ch.input)]) captureTile(d, ch);
  }
}
async function pollMotion() {
  for (const d of devices) {
    if (d.kind === 'rtsp') continue;   // URL-only streams have no ISAPI motion state
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
    // (skip paused tiles — they're excluded from all refresh)
    if (active && !prev[String(ch.input)] && !isPaused(d, ch.id)) {
      captureTile(d, ch); if (motionPopupEnabled) showMotionPopup(d, ch);
    }
  }
  // auto-close the popup once ITS channel's motion has ended (no more active state)
  if (motCur && motCur.deviceId === d.id && !channels[motCur.input]) closeImg();
}

/* ---- one image popup (#imgOverlay): used for BOTH a static event-frame lightbox
   and the live motion-detected snapshot. Close button + Esc + backdrop all work. ---- */
let motTimer = null;   // 1s image-refresh interval (live motion popup only)
let motCur = null;     // {deviceId, chId, input, name, dname, host} while showing live motion
function openImg() { $('imgOverlay').classList.add('open'); }
function closeImg() {
  if (motTimer) { clearInterval(motTimer); motTimer=null; }
  motCur = null;
  $('imgOverlay').classList.remove('open'); $('imgOverlay').classList.remove('motion');
  $('imgBig').removeAttribute('src');
}
function showImage(src, title) {       // static image (event-frame thumbnail)
  closeImg();
  $('imgTitle').textContent = title || 'Image';
  $('imgBig').src = src;
  openImg();
}
function showMotionPopup(d, ch) {
  // Don't cover a window that's already showing video (Live view or clip playback);
  // over everything else the motion alert should still pop.
  if ($('liveOverlay').classList.contains('open') || $('vidOverlay').classList.contains('open')) return;
  motCur = { deviceId:d.id, chId:ch.id, input:String(ch.input), name:ch.name, dname:d.name, host:d.host };
  $('imgOverlay').classList.add('motion');   // red accent for the alert
  refreshMotionImg();
  openImg();
  if (motTimer) clearInterval(motTimer);
  motTimer = setInterval(refreshMotionImg, 1000);   // live-update every 1s while motion lasts
}
function refreshMotionImg() {
  if (!motCur) return;
  $('imgTitle').textContent = '● Motion — '+(motCur.dname||motCur.host)+' / '+motCur.name+'  ·  '+new Date().toLocaleTimeString();
  $('imgBig').src = '/snapshot?device='+encodeURIComponent(motCur.deviceId)+'&ch='+encodeURIComponent(motCur.chId)+
                    '&res=1280x720&ts='+Date.now();
}

/* ---- event log: motion clips from the DVR's recordings ---- */
const EV = { d:null, ch:null };
async function openEvents(d, id) {
  const ch=(chans[d.id]||[]).find(c=>c.id===id); if(!ch) return;
  EV.d=d; EV.ch=ch;
  $('evTitle').textContent='Event log — '+(d.name||d.host)+' / '+ch.name;
  $('evOverlay').classList.add('open');
  loadEvents();
}
async function loadEvents() {
  if(!EV.d) return;
  $('evBody').innerHTML='<div class="diagsum">Loading motion events…</div>';
  try {
    const r=await jfetch('/events?device='+encodeURIComponent(EV.d.id)+'&ch='+encodeURIComponent(EV.ch.id)+
                         '&hours='+encodeURIComponent($('evHours').value||'24'));
    if (r.error) { $('evBody').innerHTML='<div class="chk bad">Error: '+escapeHtml(r.error)+'</div>'; return; }
    renderEvents(r.events||[]);
  } catch(e){ $('evBody').innerHTML='<div class="chk bad">Error: '+escapeHtml(e.message)+'</div>'; }
}
function renderEvents(events) {
  if (!events.length) {
    $('evBody').innerHTML='<div class="diagsum">No motion events in this window.<br>'+
      '<small style="color:#9aa3af">If it stays empty, the DVR isn\'t recording on motion — open <b>Diagnose</b> and Fix.</small></div>';
    return;
  }
  let html='<div class="diagsum">'+events.length+' motion event(s) · thumbnails load from the DVR recording (one at a time, a few seconds each):</div>';
  for (const ev of events) {
    // ONE frame pulled live from the DVR, from just after the ~10s pre-record where
    // the motion actually is. The DVR only allows one RTSP session at a time, so a
    // single lazy thumbnail per event is what loads reliably; click it to enlarge
    // (reuses the already-loaded image — instant, no second grab).
    const t=addSecsIso(ev.time, Math.min(12, Math.round(ev.seconds*0.5)));
    const thumb='<img class="evthumb" loading="lazy" src="'+dvrFrameUrl(t,'480x270')+'" '+
             'data-t="'+escapeHtml(t)+'" alt="" onerror="this.classList.add(\'evfail\')">';
    html+='<div class="evrow"><div class="evmeta"><span class="evtime">'+escapeHtml(ev.time.replace('T',' '))+'</span>'+
          '<span class="evdur">'+ev.seconds+'s</span></div>'+
          '<div class="evthumbs">'+thumb+'</div>'+
          '<button class="iconbtn" data-play="'+escapeHtml(ev.start+'|'+ev.end)+'">▶ Play</button></div>';
  }
  $('evBody').innerHTML=html;
  $('evBody').querySelectorAll('[data-play]').forEach(b=>b.onclick=()=>{ const [s,e]=b.dataset.play.split('|'); playClip(s,e); });
  // click a thumbnail -> enlarge the SAME already-loaded image (instant, cached in
  // the browser). A broken/never-loaded thumbnail has nothing to show, so skip it.
  $('evBody').querySelectorAll('.evthumb').forEach(img=>img.onclick=()=>{
    if (!img.complete || !img.naturalWidth) return;   // not loaded yet -> nothing cached
    showImage(img.currentSrc||img.src, 'Event frame · '+img.dataset.t.replace('T',' '));
  });
}
// a still grabbed live from the DVR recording at a given time (DVR wall clock)
function dvrFrameUrl(timeIso, res) {
  return '/playback?device='+encodeURIComponent(EV.d.id)+'&ch='+encodeURIComponent(EV.ch.id)+
         '&time='+encodeURIComponent(timeIso)+(res?('&res='+res):'');
}
function addSecsIso(iso, secs) {
  const d=new Date(iso); d.setSeconds(d.getSeconds()+secs);  // iso has no tz -> local wall clock (== DVR clock we send back)
  const p=x=>String(x).padStart(2,'0');
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+'T'+p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
}
function playClip(start, end) {
  const v=$('vidPlayer');
  pauseThumbs();   // give the clip the DVR's single RTSP session (stop thumbnail grabs)
  $('vidTitle').textContent='Playback — '+(EV.d.name||EV.d.host)+' / '+EV.ch.name;
  $('vidMsg').textContent='Loading… (transcoding the clip from the DVR)';
  v.src='/clip?device='+encodeURIComponent(EV.d.id)+'&ch='+encodeURIComponent(EV.ch.id)+
        '&start='+encodeURIComponent(start)+'&end='+encodeURIComponent(end);
  v.playbackRate=parseFloat($('vidSpeed').value)||1;
  $('vidOverlay').classList.add('open');
  v.onloadeddata=()=>{ $('vidMsg').textContent=''; v.playbackRate=parseFloat($('vidSpeed').value)||1; v.play().catch(()=>{}); };
  v.onerror=()=>{ $('vidMsg').textContent='⚠ could not play (DVR playback unreachable, or ffmpeg missing).'; };
}
function closeVid() {
  const v=$('vidPlayer'); v.pause(); v.removeAttribute('src'); v.load();  // stops the server-side ffmpeg
  $('vidOverlay').classList.remove('open');
  resumeThumbs();  // clip done -> let the event-log thumbnails finish loading
}
// While a clip plays it owns the DVR's one RTSP session, so pause any thumbnail
// that hasn't loaded yet (stash its src) and resume them when the clip closes.
function pauseThumbs() {
  document.querySelectorAll('.evthumb').forEach(img=>{
    if (img.complete && img.naturalWidth) return;   // already loaded -> keep showing it
    if (img.getAttribute('src')) { img.dataset.psrc=img.getAttribute('src'); img.removeAttribute('src'); }
    img.classList.remove('evfail');
  });
}
function resumeThumbs() {
  document.querySelectorAll('.evthumb').forEach(img=>{
    if (img.dataset.psrc) { img.classList.remove('evfail'); img.src=img.dataset.psrc; delete img.dataset.psrc; }
  });
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
  const clockBad = !!(rep.clock && rep.clock.fixable);
  const problems = issues.length || !rep.smtp.ok || clockBad;
  let html = '<div class="diagsum">'+(problems
    ? '⚠ '+(issues.length+(clockBad?1:0))+' issue(s) found.'
    : '✓ All good — motion will e-mail and show in the app.')+'</div>';
  // SMTP (device-wide)
  html += '<div class="diagch"><h4>E-mail (SMTP)</h4>' +
    chk(rep.smtp.ok, rep.smtp.ok
      ? 'SMTP configured — '+rep.smtp.receivers+' recipient(s)'
      : (rep.smtp.issue || 'SMTP not configured (set server + recipient on the DVR Email page)')) + '</div>';
  // DVR clock (device-wide)
  if (rep.clock && !rep.clock.unknown)
    html += '<div class="diagch"><h4>DVR clock</h4>' +
      chk(rep.clock.ok, rep.clock.ok
        ? 'Clock in sync ('+escapeHtml(rep.clock.dvr_time||'')+')'
        : (rep.clock.issue || 'DVR clock is wrong — fix to correct timestamps')) + '</div>';
  for (const c of rep.channels) {
    html += '<div class="diagch"><h4>'+escapeHtml(c.name)+' <small style="color:#9aa3af">id '+escapeHtml(c.id)+'</small></h4>';
    if (c.unused) {
      const label = c.unused==='no_camera'
        ? 'No camera on this input — empty slot (NO VIDEO)'
        : 'Hidden from the app';
      html += '<div class="chk ok"><span class="ico">–</span><span>'+escapeHtml(label)+' — motion/quality checks skipped</span></div>';
      if (c.recording_on)
        html += '<div class="chk bad"><span class="ico">✗</span><span>'+escapeHtml((c.issues[0]&&c.issues[0].msg)||'Recording is still on')+'</span></div>';
      else
        html += chk(true, 'Not recording — no wasted disk space');
      html += '</div>';
      continue;
    }
    if (!c.reachable) { html += chk(false, 'Not reachable / disabled input ('+escapeHtml(c.detail||'')+')') + '</div>'; continue; }
    html += chk(c.motion_enabled, 'Motion detection enabled');
    html += chk(c.area_painted, 'Detection area painted');
    html += chk(c.email_linked, 'E-mail on motion (email linkage)');
    html += chk(c.center_linked, 'Shows in app (Notify Surveillance Center)');
    if (c.record_mode !== undefined) {
      html += chk(c.record_mode==='motion', 'Motion-triggered recording'+(c.record_mode!=='motion'?' (now: '+escapeHtml(c.record_mode)+')':''));
      html += chk((c.pre_record||0)>=10, 'Pre-record ≥10s (now '+(c.pre_record==null?'?':c.pre_record)+'s)');
      html += chk((c.post_record||0)>=10, 'Post-record ≥10s (now '+(c.post_record==null?'?':c.post_record)+'s)');
    }
    if (c.rec_resolution !== undefined) {
      const low = (c.issues||[]).some(i=>i.code==='rec_quality_low');
      html += chk(!low, low
        ? 'Recording below HD ('+escapeHtml(c.rec_resolution)+', raising to '+escapeHtml(c.max_resolution)+')'
        : 'Recording in HD+ ('+escapeHtml(c.rec_resolution)+')');
    }
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

/* ---- settings (image save path + motion popup toggle) ---- */
let motionPopupEnabled = false;   // whether a full-screen popup pops on motion (persisted setting)
async function loadSettings() {   // at startup, so applyMotion knows before Settings is opened
  try { const s=await jfetch('/settings'); motionPopupEnabled=!!s.motion_popup; } catch (e) {}
}
async function openSettings() {
  $('setMsg').textContent=''; $('setMsg').className='mmsg';
  try {
    const s=await jfetch('/settings');
    $('setPath').value=s.save_path||''; $('setMotionPopup').checked=!!s.motion_popup;
    motionPopupEnabled=!!s.motion_popup;
  }
  catch (e) { $('setMsg').textContent='Error: '+e.message; $('setMsg').className='mmsg err'; }
  $('setOverlay').classList.add('open');
}
async function saveSettings() {
  try {
    const s=await jfetch('/settings',{method:'PUT',body:JSON.stringify({save_path:$('setPath').value.trim()})});
    $('setPath').value=s.save_path; $('setMsg').textContent='Saved: '+s.save_path; $('setMsg').className='mmsg ok';
  } catch (e) { $('setMsg').textContent='Error: '+e.message; $('setMsg').className='mmsg err'; }
}
async function saveMotionPopup() {   // persist the moment the tick changes
  const on=$('setMotionPopup').checked;
  try {
    const s=await jfetch('/settings',{method:'PUT',body:JSON.stringify({motion_popup:on})});
    motionPopupEnabled=!!s.motion_popup;
    $('setMsg').textContent='Motion popup '+(motionPopupEnabled?'on':'off'); $('setMsg').className='mmsg ok';
  } catch (e) { $('setMotionPopup').checked=!on; $('setMsg').textContent='Error: '+e.message; $('setMsg').className='mmsg err'; }
}

/* ---- live view (real-time RTSP -> MJPEG via ffmpeg) ---- */
const L = { d:null, ch:null, active:false, frames:0, audio:false };
async function openLive(d, id) {
  const ch=(chans[d.id]||[]).find(c=>c.id===id); if (!ch) return;
  L.d=d; L.ch=ch; L.active=true; L.frames=0; L.audio=!!d.audioOnly;
  // an RTSP stream is a single camera whose channel name == the device name, so don't
  // repeat it ("Audio — X / X"); a DVR channel has a distinct name worth showing.
  const dn=d.name||d.host||'';
  const label = (d.kind==='rtsp' || ch.name===dn) ? dn : (dn+' / '+ch.name);
  $('liveTitle').textContent=(L.audio?'Audio — ':'Live — ')+label;
  $('liveFps').textContent=L.audio?'audio':'…'; $('liveMsg').textContent='';
  $('liveSave').style.display = L.audio ? 'none' : '';       // no frame to save on audio-only
  $('liveStreamMeta').style.display = L.audio ? 'none' : ''; // "main stream · HD" is video-only
  $('liveOverlay').classList.toggle('audio', L.audio);
  $('liveOverlay').classList.add('open');
  if (L.audio) startAudioStream(); else startLiveStream();
}
let audioWatchdog=null;
function startAudioStream() {
  const au=$('liveAudio');
  $('liveMsg').textContent='Starting audio…'; $('liveFps').textContent='audio';
  // "playing" can fire on just the MP3 header; only real decoded audio advances the
  // clock, so treat the first timeupdate with currentTime>0 as "actually playing".
  au.ontimeupdate=()=>{ if (au.currentTime>0){ $('liveMsg').textContent=''; clearTimeout(audioWatchdog); } };
  au.onerror=()=>{ if (L.active) liveError('audio stream stopped (RTSP unreachable or ffmpeg error)'); };
  au.src='/audio?device='+encodeURIComponent(L.d.id)+'&ch='+encodeURIComponent(L.ch.id)+'&ts='+Date.now();
  au.play().catch(()=>{});   // click on "Listen" is the user gesture that allows playback
  clearTimeout(audioWatchdog);
  audioWatchdog=setTimeout(()=>{ if (L.active && L.audio && au.currentTime===0)
    liveError('no audio received — this stream isn’t sending any media (RTP timed out).'); }, 13000);
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
  L.active=false; $('liveOverlay').classList.remove('open','audio');
  const img=$('liveImg'); img.onload=img.onerror=null; img.removeAttribute('src'); // stops ffmpeg server-side
  const au=$('liveAudio'); au.ontimeupdate=au.onerror=null; au.pause(); au.removeAttribute('src'); au.load();
  clearTimeout(audioWatchdog);
  L.d=L.ch=null;
}
// fps pill only makes sense for the MJPEG video stream, not the audio player.
setInterval(()=>{ if (L.active && !L.audio) { $('liveFps').textContent=L.frames+' fps'; L.frames=0; } }, 1000);

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

$('addBtn').onclick=()=>openAddDevice();
$('addRtspBtn').onclick=()=>openAddRtsp();
$('addGroupBtn').onclick=createGroup;
$('newGroupInput').onkeydown=e=>{ if(e.key==='Enter') submitGroup(e.target.value); else if(e.key==='Escape') cancelGroup(); };
$('newGroupInput').onblur=cancelGroup;
$('dIsapi').onchange=updatePathVisibility;
$('settingsBtn').onclick=openSettings;
$('setClose').onclick=()=>$('setOverlay').classList.remove('open');
$('setSave').onclick=saveSettings;
$('setMotionPopup').onchange=saveMotionPopup;
$('diagClose').onclick=()=>$('diagOverlay').classList.remove('open');
$('diagFix').onclick=fixDiag;
$('evClose').onclick=()=>$('evOverlay').classList.remove('open');
$('evHours').onchange=loadEvents;
$('vidClose').onclick=closeVid;
$('vidSpeed').onchange=()=>{ $('vidPlayer').playbackRate=parseFloat($('vidSpeed').value)||1; };
$('imgClose').onclick=closeImg;
$('imgOverlay').onclick=e=>{ if(e.target===$('imgOverlay')) closeImg(); };  // click backdrop to close
$('devClose').onclick=()=>$('devOverlay').classList.remove('open');
$('devSave').onclick=saveDevice;
$('refresh').onclick=refreshAll;
$('auto').onchange=syncAuto; $('interval').onchange=syncAuto;
$('liveClose').onclick=closeLive;
$('liveSave').onclick=saveLiveFrame;
// Esc closes the top-most popup (video first, then any open overlay).
window.addEventListener('keydown', e=>{
  if (e.key !== 'Escape') return;
  if ($('vidOverlay').classList.contains('open')) { closeVid(); return; }
  if ($('imgOverlay').classList.contains('open')) { closeImg(); return; }
  const open = document.querySelector('.overlay.open');
  if (open) open.classList.remove('open');
});
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
loadSettings();
loadDevices();

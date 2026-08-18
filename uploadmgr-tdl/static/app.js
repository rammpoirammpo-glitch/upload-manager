async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

const $ = (id) => document.getElementById(id);
const state = {
  peers: [],
  selected: new Set(),
  msgOffset: 0,
  msgCache: [],
  qrTicker: null,
  qrTimer: null,
  transferTicker: null,
};

function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add('hidden'), 3500);
}

function show(view) {
  ['config', 'login', 'main'].forEach((v) => $('view-' + v).classList.toggle('hidden', v !== view));
}

function renderAccount(st) {
  const el = $('account');
  if (st.authorized && st.self) {
    const name = [st.self.first_name, st.self.last_name].filter(Boolean).join(' ') || st.self.username || ('#' + st.self.id);
    el.textContent = '👤 ' + name;
  } else {
    el.textContent = '';
  }
}

function fmtSize(n) {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + ' ' + u[i];
}

function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleString();
}

async function loadStatus() {
  const st = await api('/api/status');
  renderAccount(st);
  if (!st.configured) { show('config'); return; }
  if (!st.authorized) { show('login'); return; }
  show('main');
  await Promise.all([loadDialogs(), tickTransfers()]);
}

// ---- config ----
$('config-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  $('config-error').textContent = '';
  try {
    await api('/api/config', {
      method: 'POST',
      body: JSON.stringify({
        api_id: Number(fd.get('api_id')),
        api_hash: fd.get('api_hash'),
        downloads_dir: fd.get('downloads_dir'),
      }),
    });
    toast('Saved. Connecting...');
    await new Promise((r) => setTimeout(r, 1500));
    await loadStatus();
  } catch (err) {
    $('config-error').textContent = err.message;
  }
});

// ---- login tabs ----
document.querySelectorAll('.tab').forEach((b) => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x === b));
    $('tab-qr').classList.toggle('hidden', b.dataset.tab !== 'qr');
    $('tab-code').classList.toggle('hidden', b.dataset.tab !== 'code');
  });
});

function renderQR() {
  const box = $('qr-box');
  box.innerHTML = '';
  const img = document.createElement('img');
  img.src = '/api/login/qr.png?v=' + Date.now();
  img.alt = 'QR code';
  img.width = 240;
  img.height = 240;
  box.appendChild(img);
}

async function startQR() {
  $('qr-status').textContent = 'Getting QR code...';
  try {
    await api('/api/login/qr', { method: 'POST', body: '{}' });
    renderQR();
    $('qr-status').textContent = 'Scan with Telegram. Refreshes automatically.';
    // poll for acceptance
    clearInterval(state.qrTicker);
    state.qrTicker = setInterval(async () => {
      try {
        const r = await api('/api/login/qr/wait');
        if (r.ok) {
          clearInterval(state.qrTicker);
          toast('Logged in!');
          await loadStatus();
        } else if (r.needs_2fa) {
          clearInterval(state.qrTicker);
          $('pwd-pane').classList.remove('hidden');
        } else if (r.error) {
          $('qr-status').textContent = r.error;
        }
      } catch {}
    }, 800);
  } catch (err) {
    $('qr-status').textContent = err.message;
  }
}
$('qr-new').addEventListener('click', startQR);

$('code-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const phone = fd.get('phone');
  if (!fd.get('code')) {
    try {
      const d = await api('/api/login/code', { method: 'POST', body: JSON.stringify({ phone }) });
      state.codeHash = d.hash;
      $('code-input').classList.remove('hidden');
      $('code-input').querySelector('input').focus();
      $('login-status').textContent = 'Code sent to your Telegram.';
    } catch (err) { $('login-status').textContent = err.message; }
    return;
  }
  try {
    const d = await api('/api/login/code/submit', {
      method: 'POST',
      body: JSON.stringify({ phone, hash: state.codeHash, code: fd.get('code') }),
    });
    if (d.needs_2fa) { $('pwd-pane').classList.remove('hidden'); }
    else if (d.ok) { toast('Logged in!'); await loadStatus(); }
  } catch (err) { $('login-status').textContent = err.message; }
});

$('pwd-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const pwd = new FormData(e.target).get('password');
  try {
    await api('/api/login/password', { method: 'POST', body: JSON.stringify({ password: pwd }) });
    toast('Logged in!');
    await loadStatus();
  } catch (err) { toast('2FA failed: ' + err.message); }
});

// ---- dialogs / messages ----
async function loadDialogs() {
  try {
    const d = await api('/api/dialogs');
    state.peers = d.peers || [];
    const fs = $('from-select');
    fs.innerHTML = '';
    state.peers.forEach((p) => {
      const o = document.createElement('option');
      o.value = p.id;
      o.textContent = `[${p.type}] ${p.title}` + (p.id < 0 ? '' : '');
      fs.appendChild(o);
    });
    // to-select mirrors for targeting
    const ts = $('to-select');
    ts.innerHTML = '';
    state.peers.forEach((p) => {
      const o = document.createElement('option');
      o.value = p.id;
      o.textContent = `[${p.type}] ${p.title}`;
      ts.appendChild(o);
    });
  } catch (err) { toast('Dialogs: ' + err.message); }
}

async function loadMessages() {
  const peer = $('from-select').value;
  if (!peer) return;
  try {
    const d = await api('/api/messages?peer=' + peer + '&offset=' + state.msgOffset + '&limit=50');
    state.msgCache = d.messages || [];
    state.selected.clear();
    renderMessages();
  } catch (err) { toast('Messages: ' + err.message); }
}

function renderMessages() {
  const ul = $('messages');
  ul.innerHTML = '';
  state.msgCache.forEach((m) => {
    const li = document.createElement('li');
    const meta = m.has_media ? ` 📎 ${m.file_name || m.media} ${fmtSize(m.file_size)}` : '';
    const text = (m.text || '[media]').slice(0, 160);
    li.textContent = `#${m.id} · ${fmtDate(m.date)} · ${text}${meta}`;
    if (m.has_media) li.classList.add('media');
    li.addEventListener('click', () => {
      if (state.selected.has(m.id)) { state.selected.delete(m.id); li.classList.remove('sel'); }
      else { state.selected.add(m.id); li.classList.add('sel'); }
    });
    ul.appendChild(li);
  });
}

$('refresh-dialogs').addEventListener('click', loadDialogs);
$('refresh-msgs').addEventListener('click', loadMessages);
$('from-select').addEventListener('change', () => { state.msgOffset = 0; loadMessages(); });

$('download-sel').addEventListener('click', async () => {
  const peer = Number($('from-select').value);
  const ids = [...state.selected];
  if (!peer || !ids.length) return toast('Select media messages first.');
  try {
    const d = await api('/api/download', { method: 'POST', body: JSON.stringify({ peer, ids }) });
    toast('Saved ' + d.saved.length + ' file(s).');
  } catch (err) { toast('Download: ' + err.message); }
});

$('forward-sel').addEventListener('click', async () => {
  const from = Number($('from-select').value);
  const to = Number($('to-select').value);
  const ids = [...state.selected];
  if (!from || !to || !ids.length) return toast('Select messages and a target.');
  try {
    await api('/api/forward', { method: 'POST', body: JSON.stringify({ from, to, ids }) });
    toast('Forwarded ' + ids.length + ' message(s).');
  } catch (err) { toast('Forward: ' + err.message); }
});

// ---- upload ----
$('upload-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const to = Number($('to-select').value);
  const file = $('upload-file').files[0];
  if (!to || !file) return toast('Choose a chat and a file.');
  const fd = new FormData();
  fd.append('file', file);
  fd.append('caption', $('upload-caption').value);
  toast('Uploading ' + file.name + ' ...');
  try {
    const res = await fetch('/api/upload?to=' + to, { method: 'POST', body: fd });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || res.statusText);
    toast('Uploaded: ' + d.result);
  } catch (err) { toast('Upload: ' + err.message); }
});

// ---- logout ----
$('logout').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST', body: '{}' }); } catch {}
  await loadStatus();
});

// ---- activity ----
async function tickTransfers() {
  try {
    const d = await api('/api/transfers');
    const ul = $('transfers');
    ul.innerHTML = '';
    (d.transfers || []).slice(0, 20).forEach((t) => {
      const li = document.createElement('li');
      const pct = t.percent ? t.percent.toFixed(0) + '%' : '';
      li.textContent = `[${t.kind}] ${t.name} — ${t.status} ${pct} ${t.error ? '· ' + t.error : ''}`;
      li.classList.add(t.status);
      ul.appendChild(li);
    });
    clearTimeout(state.transferTicker);
    state.transferTicker = setTimeout(tickTransfers, 1500);
  } catch { clearTimeout(state.transferTicker); state.transferTicker = setTimeout(tickTransfers, 1500); }
}

loadStatus();
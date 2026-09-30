const $ = (s) => document.querySelector(s);

function fmt(n) {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < 3) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
}

async function send(tab, url, kind, btn) {
  const res = await chrome.runtime.sendMessage({ type: 'download', url, kind, referer: tab.url, title: tab.title });
  btn.textContent = res.ok ? '✓' : '✕';
  if (!res.ok) $('#status').textContent = res.error;
}

(async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

  chrome.runtime.sendMessage({ type: 'ping' }).then((r) => {
    $('#status').textContent = r.ok
      ? `Aplikasi terhubung · yt-dlp ${r.ytdlp ? 'siap' : 'belum terpasang'}`
      : 'Aplikasi PyIDM tidak berjalan';
  });

  const { media } = await chrome.runtime.sendMessage({ type: 'list', tabId: tab.id });
  const list = $('#list');
  if (!media.length) {
    list.innerHTML = '<div class="empty">Belum ada video/audio terdeteksi. Putar videonya dulu.</div>';
  }
  for (const m of [...media].sort((a, b) => b.size - a.size)) {
    const li = document.createElement('li');
    const b = document.createElement('b');
    b.textContent = m.type;
    const span = document.createElement('span');
    span.title = m.url;
    span.textContent = `${fmt(m.size)} ${decodeURIComponent(m.url.split('?')[0].split('/').pop())}`;
    const btn = document.createElement('button');
    btn.textContent = 'Download';
    btn.onclick = () => send(tab, m.url, m.kind, btn);
    li.append(b, span, btn);
    list.appendChild(li);
  }
  $('#page').onclick = (e) => send(tab, tab.url, 'page', e.target);
})();

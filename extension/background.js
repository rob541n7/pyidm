// PyIDM Integration - service worker
// 1) Menyadap response media (video/audio/HLS/DASH) per tab.
// 2) Menerima permintaan dari tombol di video / popup / menu klik kanan, lalu meneruskan ke aplikasi.

const APP = 'http://127.0.0.1:9614';
const MEDIA_EXT = /\.(mp4|webm|mkv|mov|m4v|flv|avi|3gp|mp3|m4a|aac|ogg|opus|wav|m3u8|mpd)(\?|#|$)/i;
// Potongan stream (bukan file utuh) -> abaikan
const SKIP = /\.(ts|m4s|aac\?.*seg)(\?|#|$)|[?&](range|bytestart|sq)=|googlevideo\.com\/videoplayback|\/seg-?\d+|\/frag/i;
const MIN_SIZE = 256 * 1024;

const cache = {};
async function getMedia(tabId) {
  if (!cache[tabId]) {
    const k = 't' + tabId;
    cache[tabId] = (await chrome.storage.session.get(k))[k] || [];
  }
  return cache[tabId];
}
async function saveMedia(tabId) {
  const list = cache[tabId] || [];
  await chrome.storage.session.set({ ['t' + tabId]: list });
  chrome.action.setBadgeBackgroundColor({ tabId, color: '#1a73e8' });
  chrome.action.setBadgeText({ tabId, text: list.length ? String(list.length) : '' });
}
async function clearMedia(tabId) {
  cache[tabId] = [];
  await chrome.storage.session.remove('t' + tabId);
  chrome.action.setBadgeText({ tabId, text: '' }).catch(() => {});
}

function kindOf(ct, url) {
  if (/mpegurl|\.m3u8/i.test(ct + ' ' + url)) return 'HLS';
  if (/dash\+xml|\.mpd/i.test(ct + ' ' + url)) return 'DASH';
  const m = (ct.match(/^(video|audio)\/([\w-]+)/) || [])[2] || (url.match(MEDIA_EXT) || [])[1] || 'media';
  return m.toUpperCase();
}

chrome.webRequest.onHeadersReceived.addListener(
  (d) => {
    if (d.tabId < 0 || d.statusCode >= 400) return;
    const h = {};
    for (const x of d.responseHeaders || []) h[x.name.toLowerCase()] = x.value || '';
    const ct = (h['content-type'] || '').toLowerCase();
    const isStream = /mpegurl|dash\+xml/.test(ct) || /\.(m3u8|mpd)(\?|#|$)/i.test(d.url);
    const isMedia =
      isStream ||
      (/^(video|audio)\//.test(ct) && !/mp2t|iso\.segment/.test(ct)) ||
      (MEDIA_EXT.test(d.url) && !/text\/|json|image\//.test(ct));
    if (!isMedia || SKIP.test(d.url)) return;

    let size = 0;
    const cr = h['content-range'];
    if (cr && cr.includes('/')) size = parseInt(cr.split('/').pop(), 10) || 0;
    else size = parseInt(h['content-length'], 10) || 0;
    if (!isStream && size && size < MIN_SIZE) return;

    (async () => {
      const list = await getMedia(d.tabId);
      const base = d.url.split('?')[0];
      const i = list.findIndex((m) => m.url === d.url || m.url.split('?')[0] === base);
      const item = { url: d.url, type: kindOf(ct, d.url), size, kind: isStream ? 'stream' : 'direct' };
      if (i >= 0) list[i] = { ...list[i], ...item, size: Math.max(size, list[i].size) };
      else list.push(item);
      if (list.length > 30) list.shift();
      await saveMedia(d.tabId);
    })();
  },
  { urls: ['<all_urls>'], types: ['media', 'xmlhttprequest', 'other', 'object'] },
  ['responseHeaders']
);

chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.url) clearMedia(tabId); // pindah halaman (termasuk SPA seperti YouTube)
});
chrome.tabs.onRemoved.addListener((tabId) => clearMedia(tabId));

// ---- kirim ke aplikasi
async function sendToApp({ url, kind, referer, title }) {
  const headers = { 'User-Agent': navigator.userAgent };
  if (kind !== 'page') {
    try {
      const cs = await chrome.cookies.getAll({ url });
      if (cs.length) headers.Cookie = cs.map((c) => `${c.name}=${c.value}`).join('; ');
    } catch (e) {}
  }
  // Server HLS/DASH sering memeriksa Origin & Referer dari pemutar videonya
  if (kind === 'stream' && referer) {
    try { headers.Origin = new URL(referer).origin; } catch (e) {}
  }
  const body = { url, kind, referer, headers, filename: kind === 'page' ? null : title || null };
  try {
    const r = await fetch(APP + '/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return await r.json();
  } catch (e) {
    return { ok: false, error: 'Aplikasi PyIDM tidak berjalan. Buka pyidm.py dulu.' };
  }
}

function fmtSize(n) {
  if (!n) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
}

async function candidates(tabId, src, pageUrl) {
  const out = [];
  if (/^https?:/.test(src || '')) {
    const stream = /\.(m3u8|mpd)(\?|#|$)/i.test(src);
    out.push({ url: src, kind: stream ? 'stream' : 'direct', label: 'Sumber video ini', type: stream ? 'HLS' : '' });
  }
  const fileOf = (u) => decodeURIComponent(u.split('?')[0].split('/').pop() || '');
  // Playlist master (berisi semua kualitas) diutamakan, lalu yang terbesar
  const rank = (m) => (m.kind === 'stream' ? (/master|playlist|index\.m3u8/i.test(fileOf(m.url)) ? 2 : 1) : 0);
  const sniffed = [...(await getMedia(tabId))]
    .map((m) => ({ ...m, kind: m.kind === 'page' ? 'stream' : m.kind })) // data versi lama
    .sort((a, b) => rank(b) - rank(a) || b.size - a.size);
  let recommended = false;
  for (const m of sniffed) {
    if (out.some((o) => o.url === m.url)) continue;
    let label = [fmtSize(m.size), fileOf(m.url).slice(0, 40)].filter(Boolean).join(' · ');
    if (!recommended && rank(m) === 2) { label += '  ★ direkomendasikan'; recommended = true; }
    out.push({ url: m.url, kind: m.kind, type: m.type, label });
  }
  out.push({ url: pageUrl, kind: 'page', type: 'yt-dlp', label: 'Halaman ini via yt-dlp (YouTube, dll.)' });
  return out;
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    if (msg.type === 'candidates') {
      reply({ candidates: await candidates(sender.tab.id, msg.src, msg.pageUrl) });
    } else if (msg.type === 'download') {
      reply(await sendToApp(msg));
    } else if (msg.type === 'list') {
      reply({ media: await getMedia(msg.tabId) });
    } else if (msg.type === 'ping') {
      try {
        reply(await (await fetch(APP + '/ping')).json());
      } catch (e) {
        reply({ ok: false });
      }
    }
  })();
  return true;
});

// ---- menu klik kanan
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: 'pyidm',
    title: 'Download dengan PyIDM',
    contexts: ['link', 'video', 'audio', 'page'],
  });
});
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  let url = info.srcUrl || info.linkUrl;
  let kind = 'direct';
  if (!url || url.startsWith('blob:')) { url = info.frameUrl || info.pageUrl; kind = 'page'; }
  const res = await sendToApp({ url, kind, referer: info.frameUrl || info.pageUrl, title: null });
  if (!res.ok && tab?.id != null) {
    chrome.action.setBadgeBackgroundColor({ tabId: tab.id, color: '#d93025' });
    chrome.action.setBadgeText({ tabId: tab.id, text: '!' });
  }
});

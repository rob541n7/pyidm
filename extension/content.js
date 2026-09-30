// PyIDM - tombol "Download video ini" di pojok kanan atas setiap video (seperti IDM)
(() => {
  if (window.__pyidm) return;
  window.__pyidm = true;

  const MIN_W = 200, MIN_H = 110;
  let current = null;      // <video> yang sedang ditunjuk
  let hideTimer = null;
  let menuOpen = false;

  // ---- UI dalam Shadow DOM supaya tidak terpengaruh CSS situs
  const host = document.createElement('div');
  host.style.cssText = 'all:initial;position:fixed;z-index:2147483647;top:0;left:0;display:none;';
  const root = host.attachShadow({ mode: 'closed' });
  root.innerHTML = `
    <style>
      :host { all: initial; }
      .wrap { font: 13px/1.2 "Segoe UI", system-ui, sans-serif; position: relative; }
      .btn { display:flex; align-items:center; gap:7px; padding:6px 11px 6px 8px; border-radius:6px;
        background:linear-gradient(#3d8bfd,#1a5fd1); color:#fff; cursor:pointer; user-select:none;
        box-shadow:0 2px 8px rgba(0,0,0,.45); border:1px solid rgba(255,255,255,.35); white-space:nowrap;
        opacity:.93; transition:opacity .15s, transform .15s; }
      .btn:hover { opacity:1; transform:translateY(-1px); }
      .ico { width:18px; height:18px; border-radius:4px; background:#fff; color:#1a5fd1; display:grid;
        place-items:center; font-weight:700; font-size:13px; }
      .x { margin-left:4px; opacity:.7; padding:0 2px; } .x:hover { opacity:1; }
      .menu { position:absolute; right:0; top:calc(100% + 4px); min-width:300px; max-width:440px;
        background:#fff; color:#202124; border-radius:8px; box-shadow:0 6px 24px rgba(0,0,0,.4);
        padding:4px 0; max-height:320px; overflow:auto; }
      .menu div { padding:8px 12px; cursor:pointer; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .menu div:hover { background:#e8f0fe; }
      .menu .tag { display:inline-block; min-width:44px; font-size:11px; font-weight:600; color:#1a5fd1; }
      .menu .head { font-size:11px; color:#5f6368; cursor:default; } .menu .head:hover { background:none; }
      .toast { position:absolute; right:0; top:calc(100% + 4px); padding:7px 11px; border-radius:6px;
        color:#fff; white-space:nowrap; box-shadow:0 2px 8px rgba(0,0,0,.4); }
      .ok { background:#188038; } .err { background:#d93025; }
    </style>
    <div class="wrap">
      <div class="btn" title="Download dengan PyIDM">
        <span class="ico">↓</span><span>Download video ini</span><span class="x" title="Sembunyikan">✕</span>
      </div>
    </div>`;
  const wrap = root.querySelector('.wrap');
  const btn = root.querySelector('.btn');
  const closeX = root.querySelector('.x');
  const hidden = new WeakSet(); // video yang tombolnya ditutup user

  // Jangan biarkan klik tembus ke player (supaya video tidak pause/fullscreen)
  for (const ev of ['mousedown', 'mouseup', 'pointerdown', 'pointerup', 'click', 'dblclick', 'touchstart']) {
    host.addEventListener(ev, (e) => e.stopPropagation());
  }
  host.addEventListener('mouseenter', () => clearTimeout(hideTimer));

  function mount() {
    const parent = document.fullscreenElement && document.fullscreenElement.tagName !== 'VIDEO'
      ? document.fullscreenElement : document.documentElement;
    if (host.parentNode !== parent) parent.appendChild(host);
  }

  function place() {
    if (!current || !current.isConnected) return hide(true);
    const r = current.getBoundingClientRect();
    if (r.width < MIN_W || r.height < MIN_H || r.bottom < 0 || r.top > innerHeight) return hide(true);
    host.style.top = Math.max(4, r.top + 10) + 'px';
    host.style.left = 'auto';
    host.style.right = Math.max(4, document.documentElement.clientWidth - r.right + 10) + 'px';
  }

  function show(video) {
    clearTimeout(hideTimer);
    if (current !== video) closeMenu();
    current = video;
    mount();
    host.style.display = 'block';
    place();
  }

  function hide(now) {
    clearTimeout(hideTimer);
    const go = () => { if (!menuOpen) { host.style.display = 'none'; current = null; } };
    if (now) go(); else hideTimer = setTimeout(go, 1200);
  }

  function videoAt(x, y) {
    let best = null;
    for (const v of document.querySelectorAll('video')) {
      if (hidden.has(v)) continue;
      const r = v.getBoundingClientRect();
      if (r.width < MIN_W || r.height < MIN_H) continue;
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
        if (!best || r.width * r.height < best.a) best = { v, a: r.width * r.height };
      }
    }
    return best && best.v;
  }

  // Video sering tertutup overlay transparan, jadi cek posisi kursor terhadap kotak video
  let raf = 0;
  document.addEventListener('mousemove', (e) => {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      if (e.composedPath && e.composedPath().includes(host)) return;
      const v = videoAt(e.clientX, e.clientY);
      if (v) show(v); else if (current) hide(false);
    });
  }, { passive: true, capture: true });

  addEventListener('scroll', place, { passive: true, capture: true });
  addEventListener('resize', place, { passive: true });
  document.addEventListener('fullscreenchange', () => { if (current) { mount(); place(); } });

  closeX.addEventListener('click', (e) => {
    e.stopPropagation();
    if (current) hidden.add(current);
    closeMenu();
    hide(true);
  });

  // ---- menu & toast
  function closeMenu() {
    root.querySelectorAll('.menu,.toast').forEach((n) => n.remove());
    menuOpen = false;
  }

  function toast(text, ok) {
    closeMenu();
    const t = document.createElement('div');
    t.className = 'toast ' + (ok ? 'ok' : 'err');
    t.textContent = text;
    wrap.appendChild(t);
    setTimeout(() => t.remove(), ok ? 2500 : 5000);
  }

  function showMenu(list) {
    closeMenu();
    menuOpen = true;
    const m = document.createElement('div');
    m.className = 'menu';
    const head = document.createElement('div');
    head.className = 'head';
    head.textContent = 'Pilih yang mau di-download:';
    m.appendChild(head);
    for (const c of list) {
      const row = document.createElement('div');
      row.title = c.url;
      const tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = c.type || 'FILE';
      row.append(tag, document.createTextNode(' ' + c.label));
      row.addEventListener('click', (e) => { e.stopPropagation(); send(c); });
      m.appendChild(row);
    }
    wrap.appendChild(m);
  }

  async function send(c) {
    const res = await chrome.runtime.sendMessage({
      type: 'download', url: c.url, kind: c.kind, referer: location.href,
      title: (document.title || '').replace(/\s*[-|–]\s*[^-|–]*$/, '').trim() || null,
    });
    toast(res && res.ok ? '✓ Dikirim ke PyIDM' : (res && res.error) || 'Gagal mengirim', res && res.ok);
  }

  btn.addEventListener('click', async (e) => {
    if (e.target === closeX) return;
    if (menuOpen) return closeMenu();
    if (!current) return;
    const src = current.currentSrc || current.src || current.querySelector('source')?.src || '';
    let res;
    try {
      res = await chrome.runtime.sendMessage({ type: 'candidates', src, pageUrl: location.href });
    } catch (err) {
      return toast('Ekstensi di-reload, refresh halaman ini', false);
    }
    const list = res.candidates;
    // File video langsung (mp4/webm) -> langsung download seperti IDM; selain itu tampilkan pilihan
    if (list[0].label === 'Sumber video ini' && list[0].kind === 'direct') return send(list[0]);
    showMenu(list);
  });

  document.addEventListener('mousedown', (e) => {
    if (menuOpen && !e.composedPath().includes(host)) closeMenu();
  }, true);
})();

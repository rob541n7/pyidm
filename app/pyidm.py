#!/usr/bin/env python3
"""PyIDM - download manager multi-koneksi ala IDM, dengan integrasi ekstensi browser.

- Download dipecah jadi beberapa segmen paralel (HTTP Range) + segmentasi dinamis:
  koneksi yang selesai duluan akan "membantu" segmen terbesar yang tersisa.
- Pause / resume (juga setelah aplikasi ditutup).
- Server lokal 127.0.0.1:9614 menerima perintah dari ekstensi browser.
- Streaming (HLS/DASH, YouTube, dll) ditangani lewat yt-dlp bila terpasang.
"""
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_NAME = "PyIDM"
APP_DIR = os.path.join(os.path.expanduser("~"), ".pyidm")
STATE_FILE = os.path.join(APP_DIR, "state.json")
PORT = 9614
CHUNK = 64 * 1024
MIN_SPLIT = 1024 * 1024  # segmen < 2x ini tidak dipecah lagi
MAX_RETRY = 5
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
STREAM_RE = re.compile(r"\.(m3u8|mpd)(\?|#|$)", re.I)


# ---------------------------------------------------------------- utilitas
def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "").strip(" .")
    return name[:180] or "download"


def filename_from(disposition, url):
    if disposition:
        m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", disposition, re.I)
        if m:
            return safe_name(urllib.parse.unquote(m.group(1).strip('"')))
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', disposition, re.I)
        if m:
            return safe_name(m.group(1))
    path = urllib.parse.urlparse(url).path
    return safe_name(urllib.parse.unquote(os.path.basename(path)))


def unique_path(folder, name):
    base, ext = os.path.splitext(name)
    path, i = os.path.join(folder, name), 1
    while os.path.exists(path) or os.path.exists(path + ".part"):
        path = os.path.join(folder, f"{base} ({i}){ext}")
        i += 1
    return path


def ytdlp_cmd():
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    # Terpasang lewat winget tapi PATH belum diperbarui
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
        exe = os.path.join(base, "Microsoft", "WinGet", "Links", "yt-dlp.exe") if base else ""
        if exe and os.path.isfile(exe):
            return [exe]
    exe = os.path.join(os.environ.get("ProgramFiles", ""), "WinGet", "Links", "yt-dlp.exe")
    if os.path.isfile(exe):
        return [exe]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return None


# ---------------------------------------------------------------- downloader
class Throttled(Exception):
    """Server membalas 429 Too Many Requests."""
    def __init__(self, wait):
        super().__init__("Server membatasi permintaan (429)")
        self.wait = wait


def retry_after(err, default):
    try:
        return max(1, min(120, int(err.headers.get("Retry-After", default))))
    except (TypeError, ValueError, AttributeError):
        return default


class Segment:
    def __init__(self, start, end, done=0):
        self.start, self.end, self.done = start, end, done  # end=-1 -> ukuran tak diketahui
        self.active = False

    @property
    def remaining(self):
        return self.end - (self.start + self.done) + 1 if self.end >= 0 else 1

    @property
    def complete(self):
        return self.end >= 0 and self.remaining <= 0


class Download:
    def __init__(self, url, folder, conns=8, filename=None, headers=None,
                 referer=None, mode="http", id=None):
        self.id = id or uuid.uuid4().hex[:10]
        self.url, self.folder, self.conns = url, folder, conns
        self.filename, self.headers, self.referer = filename, headers or {}, referer
        self.title = filename  # judul dari browser (dipakai sebagai nama file yt-dlp)
        self.mode = mode  # "http" atau "ytdlp"
        self.path = None
        self.total = 0
        self.resumable = False
        self.segments = []
        self.status = "Antri"
        self.error = ""
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.proc = None
        self.yt_percent = 0.0
        self.yt_size = ""
        self.info = ""  # pesan terakhir yt-dlp (coba ulang, peringatan) untuk kolom Status
        self.speed = 0.0
        self._last = (time.time(), 0)

    # ---- info untuk GUI
    @property
    def downloaded(self):
        return sum(s.done for s in self.segments)

    @property
    def percent(self):
        if self.mode == "ytdlp":
            return self.yt_percent
        if self.status == "Selesai":
            return 100.0
        return self.downloaded * 100 / self.total if self.total else 0.0

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    @property
    def part(self):
        return self.path + ".part"

    # ---- kontrol
    def start(self):
        if self.running or self.status == "Selesai":
            return
        self.stop.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def pause(self):
        self.stop.set()
        self._kill()

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            if os.name == "nt":  # hentikan juga proses anak (winget memakai launcher)
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                self.proc.terminate()

    # ---- HTTP
    def _req(self, extra=None):
        h = {"User-Agent": UA, "Accept": "*/*"}
        h.update(self.headers)
        if self.referer:
            h["Referer"] = self.referer
        if extra:
            h.update(extra)
        return urllib.request.Request(self.url, headers=h)

    def _open_probe(self):
        """Koneksi pertama, dengan beberapa kali coba ulang kalau timeout / terputus."""
        for attempt in range(4):
            try:
                return urllib.request.urlopen(self._req({"Range": "bytes=0-"}), timeout=30)
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    raise IOError("Ditolak server (403). Link kemungkinan kedaluwarsa: "
                                  "refresh halaman, putar videonya, lalu download lagi.") from e
                if e.code == 429 and attempt < 3:
                    wait = retry_after(e, 10 * (attempt + 1))
                    self.status = f"Server membatasi (429), menunggu {wait} detik"
                    self.stop.wait(wait)
                    continue
                if e.code < 500 or attempt == 3:
                    raise
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                if attempt == 3 or self.stop.is_set():
                    host = urllib.parse.urlparse(self.url).hostname
                    reason = getattr(e, "reason", e)
                    raise IOError(f"Tidak bisa terhubung ke {host} setelah 4x coba ({reason})") from e
            self.status = f"Menghubungkan (coba ulang {attempt + 1})"
            time.sleep(3 * (attempt + 1))

    def _probe(self):
        with self._open_probe() as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            cr = r.headers.get("Content-Range") or ""
            if r.status == 206 and cr.split("/")[-1].strip().isdigit():
                self.total, self.resumable = int(cr.split("/")[-1]), True
            else:
                self.total, self.resumable = int(r.headers.get("Content-Length") or 0), False
            server_name = filename_from(r.headers.get("Content-Disposition"), r.geturl())
        # Halaman web / playlist streaming -> serahkan ke yt-dlp
        if ctype in ("text/html", "application/vnd.apple.mpegurl", "application/x-mpegurl",
                     "application/dash+xml") and ytdlp_cmd():
            self.mode = "ytdlp"
            return
        ext = os.path.splitext(server_name)[1]
        if not ext or len(ext) > 6:
            ext = mimetypes.guess_extension(ctype) or ""
        if self.filename:  # judul dari ekstensi
            name = safe_name(self.filename)
            if not os.path.splitext(name)[1] or len(os.path.splitext(name)[1]) > 6:
                name += ext
        else:
            name = server_name if os.path.splitext(server_name)[1] else server_name + ext
        self.path = unique_path(self.folder, name)
        self.filename = os.path.basename(self.path)

    def _next_segment(self):
        """Ambil segmen belum selesai yang menganggur, atau pecah segmen aktif terbesar."""
        with self.lock:
            for s in self.segments:
                if not s.active and not s.complete:
                    s.active = True
                    return s
            if not self.resumable:
                return None
            big = max((s for s in self.segments if s.active), key=lambda s: s.remaining, default=None)
            if not big or big.remaining < 2 * MIN_SPLIT:
                return None
            mid = big.start + big.done + big.remaining // 2
            new = Segment(mid, big.end)
            big.end = mid - 1
            new.active = True
            self.segments.append(new)
            return new

    def _worker(self):
        seg = self._next_segment()
        throttles = 0
        while seg and not self.stop.is_set():
            try:
                self._fetch(seg)
            except Throttled as t:
                # Server membatasi: koneksi ini mundur. Kalau masih ada koneksi lain,
                # segmennya diambil alih mereka (jumlah koneksi berkurang otomatis).
                seg.active = False
                with self.lock:
                    others = sum(1 for s in self.segments if s.active)
                if others:
                    return
                throttles += 1
                if throttles > 6:
                    self.error = "Server terus membatasi (429). Tunggu beberapa menit lalu klik Lanjutkan."
                    return
                self.info = f"Server membatasi (429), menunggu {t.wait} detik"
                self.stop.wait(t.wait)
                self.info = ""
                seg = self._next_segment()
                continue
            except Exception as e:  # segmen gagal permanen
                self.error = str(e)
                seg.active = False
                return
            seg.active = False
            seg = self._next_segment()

    def _fetch(self, seg):
        retries = 0
        while not self.stop.is_set() and not seg.complete:
            pos = seg.start + seg.done
            extra = {"Range": f"bytes={pos}-{seg.end}"} if self.resumable else {}
            try:
                with urllib.request.urlopen(self._req(extra), timeout=30) as r, \
                        open(self.part, "r+b") as f:
                    if self.resumable and r.status != 206:
                        raise IOError("Server berhenti mendukung Range")
                    f.seek(pos)
                    while not self.stop.is_set():
                        data = r.read(CHUNK)
                        if not data:
                            if seg.end < 0:  # ukuran tidak diketahui: selesai saat stream habis
                                seg.end = seg.start + seg.done - 1
                                self.total = seg.done
                            break
                        with self.lock:  # end bisa dipotong oleh _next_segment
                            if seg.end >= 0:
                                data = data[:max(0, seg.remaining)]
                            seg.done += len(data)
                        f.write(data)
                        if seg.complete:
                            return
                        retries = 0
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    raise Throttled(retry_after(e, 15)) from e
                if e.code < 500 or not self.resumable or retries >= MAX_RETRY:
                    raise
                retries += 1
                time.sleep(2 * retries)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                if not self.resumable or retries >= MAX_RETRY:
                    raise
                retries += 1
                time.sleep(2 * retries)

    def _run(self):
        try:
            self.error = ""
            self.status = "Menghubungkan"
            if self.mode == "http" and not self.segments:
                self._probe()
                if self.mode == "http":
                    os.makedirs(self.folder, exist_ok=True)
                    with open(self.part, "wb") as f:
                        if self.total:
                            f.truncate(self.total)
                    if self.resumable and self.total:
                        n = max(1, min(self.conns, self.total // MIN_SPLIT))
                        size = self.total // n
                        self.segments = [Segment(i * size, self.total - 1 if i == n - 1 else (i + 1) * size - 1)
                                         for i in range(n)]
                    else:
                        self.segments = [Segment(0, self.total - 1 if self.total else -1)]
            if self.mode == "ytdlp":
                return self._run_ytdlp()

            if not self.resumable:  # server tanpa Range: mulai ulang dari nol
                self.segments = [Segment(0, self.total - 1 if self.total else -1)]
                open(self.part, "wb").close()
            if not os.path.exists(self.part):
                raise IOError("File .part hilang, hapus lalu tambahkan ulang")
            for s in self.segments:
                s.active = False

            self.status = "Mengunduh"
            workers = [threading.Thread(target=self._worker, daemon=True)
                       for _ in range(self.conns if self.resumable else 1)]
            for w in workers:
                w.start()
                time.sleep(0.05)
            for w in workers:
                w.join()

            if self.stop.is_set():
                self.status = "Dijeda"
            elif all(s.complete for s in self.segments):
                os.replace(self.part, self.path)
                self.status = "Selesai"
            else:
                self.status = "Gagal"
                self.error = self.error or "Koneksi terputus"
        except Exception as e:
            self.status, self.error = "Gagal", str(e)

    def _run_ytdlp(self):
        cmd = ytdlp_cmd()
        if not cmd:
            raise RuntimeError("yt-dlp belum terpasang (jalankan: pip install yt-dlp)")
        os.makedirs(self.folder, exist_ok=True)
        self.status = "Mengunduh"
        n = self.conns
        while True:
            rc, last, throttled = self._ytdlp_once(cmd, n)
            if throttled and n > 1 and not self.stop.is_set():
                # Server membatasi (429): ulangi dengan koneksi lebih sedikit.
                # yt-dlp melanjutkan dari potongan yang sudah terunduh.
                n = max(1, n // 2)
                self.info = f"Server membatasi (429) - turun ke {n} koneksi, menunggu 15 detik"
                self.stop.wait(15)
                continue
            break
        self.speed = 0
        self.info = ""
        if self.stop.is_set():
            self.status = "Dijeda"
        elif rc == 0:
            self.status, self.yt_percent = "Selesai", 100.0
        elif "Unsupported URL" in last:
            self.status = "Gagal"
            self.error = ("Situs ini tidak didukung yt-dlp. Putar videonya, lalu pilih stream "
                          "HLS (master.m3u8) di menu tombol download.")
        elif "429" in last:
            self.status = "Gagal"
            self.error = "Server membatasi permintaan (429). Tunggu beberapa menit lalu klik Lanjutkan."
        else:
            self.status, self.error = "Gagal", last

    def _ytdlp_once(self, cmd, n):
        """Jalankan yt-dlp sekali. Hasil: (kode keluar, baris terakhir, kena 429 dengan >1 koneksi)."""
        name = safe_name(self.title) + ".%(ext)s" if self.title else "%(title).150B [%(id)s].%(ext)s"
        args = cmd + ["--newline", "--no-playlist", "--no-mtime", "-N", str(n),
                      "--socket-timeout", "20", "--retries", "10", "--fragment-retries", "10",
                      "--retry-sleep", "http:exp=2:60", "--retry-sleep", "fragment:exp=2:60",
                      "-o", os.path.join(self.folder, name)]
        if self.referer:
            args += ["--referer", self.referer]
        for k, v in self.headers.items():
            args += ["--add-header", f"{k}:{v}"]
        args.append(self.url)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", creationflags=flags)
        last = ""
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            last = line
            if n > 1 and re.search(r"\b429\b|Too Many Requests", line):
                self._kill()
                self.proc.wait()
                return 1, line, True
            if re.search(r"Got error|Retrying|^WARNING|^ERROR", line):
                self.info = re.sub(r"^\[\w+\]\s*", "", line)
            m = re.search(r"\[download\]\s+([\d.]+)% of\s+~?\s*([\d.]+\s*\w+)", line)
            if m:
                self.info = ""
                self.yt_percent, self.yt_size = float(m.group(1)), m.group(2)
                sm = re.search(r" at\s+([\d.]+)\s*(\w)i?B/s", line)
                mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
                self.speed = float(sm.group(1)) * mult.get(sm.group(2), 1) if sm else 0
            for pat in (r"Destination: (.+)$", r'Merging formats into "(.+)"$',
                        r"\[download\] (.+) has already been downloaded"):
                m = re.search(pat, line)
                if m:
                    self.path = m.group(1)
                    self.filename = os.path.basename(self.path)
        return self.proc.wait(), last, False

    # ---- simpan / muat
    def to_dict(self):
        return {k: getattr(self, k) for k in ("id", "url", "folder", "conns", "filename", "headers", "referer",
                                              "mode", "path", "total", "resumable", "status", "error", "title",
                                              "yt_percent", "yt_size")} | {
            "segments": [[s.start, s.end, s.done] for s in self.segments]}

    @classmethod
    def from_dict(cls, d):
        o = cls(d["url"], d["folder"], d["conns"], d["filename"], d["headers"], d["referer"], d["mode"], d["id"])
        for k in ("path", "total", "resumable", "status", "error", "yt_percent", "yt_size", "title"):
            setattr(o, k, d.get(k, getattr(o, k)))
        o.segments = [Segment(*s) for s in d.get("segments", [])]
        if o.status in ("Mengunduh", "Menghubungkan", "Antri"):
            o.status = "Dijeda"
        return o


# ---------------------------------------------------------------- server ekstensi
class Handler(BaseHTTPRequestHandler):
    inbox = None  # queue.Queue, diisi oleh App

    def _origin_ok(self):
        # Tolak request dari halaman web biasa; hanya ekstensi browser / lokal.
        origin = self.headers.get("Origin", "")
        return not origin or origin.startswith(("chrome-extension://", "moz-extension://", "extension://"))

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/ping" and self._origin_ok():
            return self._send(200, {"ok": True, "app": APP_NAME, "ytdlp": bool(ytdlp_cmd())})
        self._send(404, {"ok": False})

    def do_POST(self):
        if self.path != "/download" or not self._origin_ok():
            return self._send(403, {"ok": False, "error": "forbidden"})
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if not str(data.get("url", "")).startswith(("http://", "https://")):
                raise ValueError("URL tidak valid")
            self.inbox.put(data)
            self._send(200, {"ok": True})
        except Exception as e:
            self._send(400, {"ok": False, "error": str(e)})

    def log_message(self, *a):
        pass


# ---------------------------------------------------------------- GUI
class App:
    COLS = (("name", "Nama File", 330), ("size", "Ukuran", 90), ("progress", "Progres", 190),
            ("speed", "Kecepatan", 95), ("status", "Status", 200))

    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME} - Download Manager")
        root.geometry("960x480")
        os.makedirs(APP_DIR, exist_ok=True)
        self.settings = {"folder": os.path.join(os.path.expanduser("~"), "Downloads", APP_NAME), "conns": 8}
        self.downloads = {}
        self.inbox = queue.Queue()
        self._load()

        bar = ttk.Frame(root, padding=(6, 6, 6, 0))
        bar.pack(fill="x")
        for text, cmd in (("➕ Tambah URL", self.add_url), ("▶ Lanjutkan", self.resume_sel),
                          ("⏸ Jeda", self.pause_sel), ("⏯ Semua", self.toggle_all),
                          ("🗑 Hapus", self.delete_sel), ("📂 Buka Folder", self.open_folder),
                          ("⚙ Pengaturan", self.open_settings)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)

        frame = ttk.Frame(root, padding=6)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in self.COLS], show="headings", selectmode="extended")
        for key, title, w in self.COLS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=w, anchor="w" if key in ("name", "status") else "center")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.open_file)
        self.tree.bind("<Button-3>", self.context_menu)

        self.statusbar = ttk.Label(root, anchor="w", padding=(8, 2))
        self.statusbar.pack(fill="x")

        for d in self.downloads.values():
            self.tree.insert("", "end", iid=d.id)

        Handler.inbox = self.inbox
        self.server_ok = True
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        except OSError:
            self.server_ok = False
        self._last_save = 0
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tick()

    # ---- state
    def _load(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                st = json.load(f)
            self.settings.update(st.get("settings", {}))
            for d in st.get("downloads", []):
                o = Download.from_dict(d)
                self.downloads[o.id] = o
        except (OSError, ValueError, KeyError):
            pass

    def _save(self):
        st = {"settings": self.settings, "downloads": [d.to_dict() for d in self.downloads.values()]}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1)
        os.replace(tmp, STATE_FILE)

    # ---- tambah download
    def add(self, url, filename=None, referer=None, headers=None, kind="direct"):
        mode = "ytdlp" if kind in ("page", "stream") or STREAM_RE.search(url) else "http"
        d = Download(url, self.settings["folder"], int(self.settings["conns"]), filename,
                     headers, referer, mode)
        self.downloads[d.id] = d
        self.tree.insert("", 0, iid=d.id)
        d.start()
        self._save()

    def add_url(self):
        try:
            clip = self.root.clipboard_get().strip()
        except tk.TclError:
            clip = ""
        url = simpledialog.askstring(APP_NAME, "URL yang mau di-download:", parent=self.root,
                                     initialvalue=clip if clip.startswith("http") else "")
        if url and url.strip().startswith(("http://", "https://")):
            self.add(url.strip())

    def _selected(self):
        return [self.downloads[i] for i in self.tree.selection() if i in self.downloads]

    def resume_sel(self):
        for d in self._selected():
            if d.status != "Selesai":
                d.start()

    def pause_sel(self):
        for d in self._selected():
            d.pause()

    def toggle_all(self):
        active = [d for d in self.downloads.values() if d.running]
        if active:
            for d in active:
                d.pause()
        else:
            for d in self.downloads.values():
                if d.status in ("Dijeda", "Gagal"):
                    d.start()

    def delete_sel(self):
        sel = self._selected()
        if not sel:
            return
        also_file = messagebox.askyesnocancel(APP_NAME, f"Hapus {len(sel)} item dari daftar.\n\n"
                                              "Hapus juga file yang belum selesai (.part)?")
        if also_file is None:
            return
        for d in sel:
            d.pause()
            if d.thread:
                d.thread.join(timeout=3)
            if also_file and d.path and os.path.exists(d.path + ".part"):
                try:
                    os.remove(d.path + ".part")
                except OSError:
                    pass
            self.tree.delete(d.id)
            del self.downloads[d.id]
        self._save()

    def open_folder(self):
        sel = self._selected()
        folder = os.path.dirname(sel[0].path) if sel and sel[0].path else self.settings["folder"]
        os.makedirs(folder, exist_ok=True)
        self._open(folder)

    def open_file(self, _e=None):
        sel = self._selected()
        if sel and sel[0].status == "Selesai" and sel[0].path and os.path.exists(sel[0].path):
            self._open(sel[0].path)

    @staticmethod
    def _open(path):
        if os.name == "nt":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])

    def context_menu(self, e):
        row = self.tree.identify_row(e.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        d = self.downloads[row]
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Buka file", command=self.open_file)
        m.add_command(label="Buka folder", command=self.open_folder)
        m.add_separator()
        m.add_command(label="Lanjutkan", command=self.resume_sel)
        m.add_command(label="Jeda", command=self.pause_sel)
        m.add_command(label="Salin URL", command=lambda: (self.root.clipboard_clear(),
                                                           self.root.clipboard_append(d.url)))
        if d.error:
            m.add_command(label="Lihat error", command=lambda: messagebox.showerror(APP_NAME, d.error))
        m.add_separator()
        m.add_command(label="Hapus", command=self.delete_sel)
        m.tk_popup(e.x_root, e.y_root)

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("Pengaturan")
        win.transient(self.root)
        win.resizable(False, False)
        folder = tk.StringVar(value=self.settings["folder"])
        conns = tk.IntVar(value=self.settings["conns"])
        ttk.Label(win, text="Folder download:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(win, textvariable=folder, width=48).grid(row=0, column=1, padx=4)
        ttk.Button(win, text="...", width=3, command=lambda: folder.set(
            filedialog.askdirectory(initialdir=folder.get()) or folder.get())).grid(row=0, column=2, padx=8)
        ttk.Label(win, text="Koneksi per file:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        ttk.Spinbox(win, from_=1, to=32, textvariable=conns, width=6).grid(row=1, column=1, sticky="w", padx=4)

        def ok():
            self.settings.update(folder=folder.get(), conns=max(1, min(32, conns.get())))
            self._save()
            win.destroy()
        ttk.Button(win, text="Simpan", command=ok).grid(row=2, column=1, sticky="e", pady=8)

    # ---- loop GUI
    def tick(self):
        while not self.inbox.empty():
            data = self.inbox.get()
            self.add(data["url"], data.get("filename"), data.get("referer"),
                     data.get("headers") or {}, data.get("kind", "direct"))
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(300, lambda: self.root.attributes("-topmost", False))

        now = time.time()
        total_speed = 0
        for d in self.downloads.values():
            if d.mode == "http":
                t0, b0 = d._last
                if now - t0 >= 1:
                    cur = d.downloaded
                    inst = max(0, cur - b0) / (now - t0)
                    d.speed = inst if not d.running else 0.6 * inst + 0.4 * d.speed
                    d._last = (now, cur)
                if not d.running:
                    d.speed = 0
            pct = d.percent
            bar = "█" * int(pct // 5) + "░" * (20 - int(pct // 5))
            if d.mode == "ytdlp":
                size = d.yt_size or "?"
            else:
                size = human(d.total) if d.total else (human(d.downloaded) if d.downloaded else "?")
            status = d.status
            if d.status == "Mengunduh" and d.mode == "http":
                n = sum(1 for s in d.segments if s.active)
                status += f" ({n} koneksi)"
                if d.info:
                    status += f" · {d.info[:70]}"
                if d.speed > 0 and d.total:
                    eta = (d.total - d.downloaded) / d.speed
                    status += f" · {int(eta // 60)}m{int(eta % 60):02d}s"
            elif d.status == "Mengunduh" and d.info:
                status += f" · {d.info[:90]}"
            elif d.status == "Gagal" and d.error:
                status += f": {d.error[:80]}"
            total_speed += d.speed if d.running else 0
            self.tree.item(d.id, values=(d.filename or d.url, size, f"{bar} {pct:5.1f}%",
                                         f"{human(d.speed)}/s" if d.running and d.speed else "",
                                         status))

        srv = f"Ekstensi: aktif di 127.0.0.1:{PORT}" if self.server_ok else \
            f"Ekstensi: port {PORT} dipakai (PyIDM lain sedang jalan?)"
        yt = "yt-dlp: siap" if ytdlp_cmd() is not None else "yt-dlp: belum terpasang (pip install yt-dlp)"
        self.statusbar.config(text=f"{srv}   |   {yt}   |   Total: {human(total_speed)}/s")

        if now - self._last_save > 5:
            self._last_save = now
            self._save()
        self.root.after(500, self.tick)

    def on_close(self):
        for d in self.downloads.values():
            d.pause()
        for d in self.downloads.values():
            if d.thread:
                d.thread.join(timeout=3)
        self._save()
        self.root.destroy()


if __name__ == "__main__":
    ytdlp_cmd = __import__("functools").lru_cache(maxsize=1)(ytdlp_cmd)
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()

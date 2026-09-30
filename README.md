# PyIDM — Download Manager + Ekstensi Browser

## 1. Jalankan aplikasi
```
pip install yt-dlp        (opsional, untuk YouTube / HLS / situs streaming)
python app\pyidm.py        (atau klik ganda jalankan.bat)
```
Aplikasi harus terbuka supaya ekstensi bisa mengirim download (server lokal di 127.0.0.1:9614).

## 2. Pasang ekstensi (Chrome / Edge / Brave)
1. Buka `chrome://extensions` (Edge: `edge://extensions`)
2. Aktifkan **Developer mode**
3. Klik **Load unpacked**, lalu pilih folder `extension`
4. Refresh tab yang sudah terbuka

## Cara pakai
- Arahkan kursor ke video, lalu klik tombol **"Download video ini"** di pojok kanan atasnya.
  - File video langsung (mp4/webm) langsung dikirim ke aplikasi.
  - Video streaming (blob/HLS/DASH) memunculkan menu pilihan: stream yang terdeteksi, atau "Halaman ini (via yt-dlp)".
  - ✕ di tombol menyembunyikannya untuk video tersebut.
- Ikon ekstensi: daftar semua media yang terdeteksi di tab (angka pada badge = jumlahnya).
- Klik kanan pada link/video → **Download dengan PyIDM**.

## Fitur aplikasi
- Hingga 32 koneksi per file, dengan segmentasi dinamis: koneksi yang selesai duluan membantu bagian yang tersisa
- Pause/resume, juga setelah aplikasi ditutup (state disimpan di `~/.pyidm/state.json`)
- Cookie, Referer, dan User-Agent diteruskan dari browser, jadi link yang butuh login tetap bisa
- Klik kanan pada item untuk buka file/folder, salin URL, dan lihat error
- Server lokal hanya menerima request dari ekstensi (request dari website biasa ditolak)

## Batasan
- Video ber-DRM (Netflix, Disney+, Spotify, dll.) tidak bisa di-download.
- Gunakan hanya untuk konten yang boleh Anda unduh; ikuti hak cipta dan ketentuan situsnya.

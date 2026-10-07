# Web UI — dashboard AI scalper

Single-page dashboard untuk memantau dan mengendalikan bot. Dibangun di atas **aiohttp** (sudah
ada di env conda hummingbot) dengan frontend statis vanilla HTML/JS/CSS — tanpa build step, tanpa
CDN, tanpa dependensi tambahan di VPS.

```
browser ──HTTP──► server.py ──subprocess──► hbot (status/history/config/logs/doctor/start/stop)
                   │
                   └──aiomqtt──► Mosquitto hbot/predictions/#  (sinyal ML, read-only)
```

Sebuah *poller* di background menjalankan perintah `hbot` dan menyimpan hasilnya di memori, jadi
HTTP handler tidak pernah memblokir pada subprocess (`hbot status` cold-start bisa makan
berdetik-detik).

## Yang ditampilkan

| Panel | Isi |
|---|---|
| AI signal | Arah (LONG/SHORT/FLAT), confidence, probabilitas short/neutral/long, `target_pct`, umur sinyal, status stale, sinyal rusak yang dibuang, sparkline riwayat, pair |
| Bot control | Start / Stop / Kill switch (Stop membatalkan order; Kill switch juga menyetel `manual_kill_switch`) |
| Strategy status | Output `format_status` controller — termasuk baris `signal/confidence/stale/age/dropped` dan `target_pct/sl/tp/time_limit/entry` dari `ai_scalper` |
| Performance | Net/gross/fees/trades/return% + bar chart + tabel per market, setelah fee |
| Live config | Semua field yang controller tandai `is_updatable`, lengkap batas [min,max], edit & apply live (~10s) |
| Logs | Tail log terstruktur, baris ERROR/WARN diwarnai, auto-refresh |
| Health | `hbot doctor` + snapshot balance |

## Menjalankan

**Lokal (Windows, tanpa bot) — preview mode demo dengan telemetri sintetis:**

```bash
cd ai_scalper/webui
python server.py --demo            # buka http://127.0.0.1:8080
```

Semua panel bergerak seperti live, tapi tombol kontrol dimatikan (HTTP 403). Mode demo juga
otomatis aktif jika `hbot` tidak ditemukan di PATH.

**VPS (live):**

```bash
cd ~/hummingbot && conda activate hummingbot
python ai_scalper/webui/server.py --mqtt-host localhost
```

Atau sebagai service (diinstal oleh `setup_vps.sh --install-services`):

```bash
sudo systemctl start webui
sudo systemctl enable webui
```

Lalu dari laptop: `ssh -L 8080:127.0.0.1:8080 user@vps` dan buka `http://127.0.0.1:8080`.

## Keamanan

- Default bind **127.0.0.1**. Bind non-loopback tanpa token **ditolak** (exit 2) — UI ini bisa
  menstop dan me-retune bot yang pegang dana nyata. Gunakan SSH tunnel, atau `--token <secret>`
  (`WEBUI_TOKEN` di `.env` juga berlaku) lalu kirim sebagai header `X-WebUI-Token` atau query
  `?token=`.
- `POST` (mutasi) selalu melewati: token → whitelist key (intersect field `is_updatable` + bounds
  server) → validasi nilai (range/enum/bool/cap `total_amount_quote`) → argv list, bukan shell
  string. `GET` (baca) tidak butuh token.
- Nama config `start` divalidasi regex `[A-Za-z0-9_.\-]+\.yml` — traversal path dan shell
  metacharacter ditolak sebelum subprocess jalan.

## API

| Endpoint | Metode | Keterangan |
|---|---|---|
| `/api/health` | GET | mode, hbot ada/tidak, status MQTT, token-required |
| `/api/state` | GET | status bot + sinyal AI (MQTT di-enrich threshold live) + `live_fields` |
| `/api/performance` | GET | baris history + ringkasan (net/gross/fees/return) |
| `/api/config` | GET | global + strategy config + daftar tunables + bounds |
| `/api/logs` | GET | tail log terstruktur |
| `/api/doctor` | GET | hasil `hbot doctor` |
| `/api/config` | POST | `{key, value}` → `hbot config` (divalidasi) |
| `/api/start` | POST | `{config?}` → `hbot start` |
| `/api/stop` | POST | `{force?}` → `hbot stop` |
| `/api/kill` | POST | kill switch → `manual_kill_switch` + stop |

Semua payload JSON; nilai non-JSON diserialisasi sebagai string.

## Catatan desain

- Sinyal di UI dihitung dari probabilitas mentah + threshold live dari config — UI dan controller
  menampilkan keputusan yang sama, karena keduanya membaca sumber yang sama.
- Sparkline dibangun dari buffer polling di sisi browser (120 titik terakhir), jadi bekerja di
  mode demo maupun live tanpa endpoint tambahan.
- Panel config hanya menampilkan field yang punya bounds di server (`hbot_client.py`); field
  live-updatable lain (`activation_bounds`, `trailing_stop`) ditampilkan sebagai read-only beserta
  alasannya.

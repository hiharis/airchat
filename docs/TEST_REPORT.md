# AirChat — Test Report & Verification Summary

**Date:** 2026-10-12  
**Environment:** Windows 10/11, Python 3.10.0, FastAPI 0.142.3, Uvicorn 0.54.0, aiosqlite 0.22.1  
**Target Repository:** `https://github.com/hiharis/airchat.git`  
**Branch:** `main`

---

## 1. Feature Presence

| Feature | Status | Notes |
|---|---|---|
| Offline Local Chat & History | **Present** | Auto-incrementing IDs, message persistence, full and delta history replay |
| Presence & User List | **Present** | Online count, drawer/sidebar user list, multi-tab per user support |
| File & Media Sharing (up to 20 MB) | **Present** | Any file type up to 20 MiB, safe Content-Disposition & MIME headers |
| Client-Side Image Compression | **Present** | Auto-resizes camera photos with an explicit HD toggle for original files |
| Typing Indicator | **Present** | `{"type":"typing"}` broadcast to others only, rate-limited, auto-expires |
| Emoji Reactions | **Present** | Whitelist (`👍`, `❤️`, `😂`, `😮`, `😢`, `🔥`), togglable, capped at 12 per message |
| Delta Reconnect (`?after=<id>`) | **Present** | Downloads only missed messages after reconnect, avoids full replay |
| Connection Hardening | **Present** | Per-socket outbound queue (max 100), send timeouts (5 s), connection cap (50), upload semaphore (3) |
| Liquid Glass UI (Dark/Light) | **Present** | Apple-inspired translucent glass panels, mobile drawer, responsive controls |
| Optional `hello` Handshake | **MISSING** | Not implemented in protocol; standard WebSocket upgrade used |

---

## 2. Test Execution & Verification

### Automated Test Suite (`pytest`)
- Total Tests: **26 passing** across all test modules
  - `tests/test_upload.py`: 11 passed (file size limits, 20 MB cap, path traversal resistance, safe serving headers)
  - `tests/test_realtime.py`: 10 passed (history replay, delta reconnect, typing indicator, reactions, connection caps)
  - `tests/test_presence_and_migration.py`: 5 passed (multi-tab presence, username validation, old-schema migration, uvicorn integration)

### Load & Stress Simulation (`tests/load_sim.py`)
- **30 simultaneous clients** sending 1 message/second for 20 seconds (18,000 deliveries).
- **1 stalled client** (zero reads): safely dropped in **2.44 s** (threshold: ≤ 10.0 s).
- **Median message delivery latency:** **217.38 ms** (threshold: < 500 ms).
- **Server exceptions / crashes:** **0**.

---

## 3. Security & Static Analysis
- **XSS Prevention:** Zero unescaped `innerHTML` with user content; all chat text, usernames, and filenames are DOM-rendered via `textContent` or `createElement`.
- **Offline Integrity:** Zero external CDN, font, or script calls; 100% self-contained offline assets.
- **Path Traversal:** Directory traversal probes (`..%2f`, null bytes, absolute paths) return 400/404 and are blocked from escaping `uploads/`.
- **Database Safety:** SQLite WAL mode and `PRAGMA busy_timeout=5000` protect against concurrency contention.

---

## 4. Known Limitations
- Native browser push notifications require an HTTPS context or `localhost` (browser security restriction); fallback in-page audio and tab title badge notifications function across plain HTTP LAN connections.

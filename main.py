import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

import qrcode
import uvicorn
from fastapi import (
    FastAPI, File, Form, HTTPException, Response, UploadFile,
    WebSocket, WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from database import (
    get_history, get_messages_delta, init_db, save_message, toggle_reaction
)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
PORT = 9999                          # static port so the URL never changes
UPLOAD_DIR = Path("uploads")
MAX_UPLOAD_MB = 20
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHARS = 4000
CHUNK_SIZE = 1024 * 1024
USERNAME_RE = re.compile(r"^[\w .\-]{1,32}$")   # letters, digits, _ space . -

ALLOWED_EMOJIS = {"👍", "❤️", "😂", "😮", "😢", "🔥"}

COMPOUND_EXTS = (".tar.gz", ".tar.bz2", ".tar.xz")
INLINE_SAFE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".pdf"}
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# --------------------------------------------------------------------------
# Logging: throttle noisy "Invalid HTTP request" warnings (e.g. TLS probes)
# --------------------------------------------------------------------------
class DuplicateHttpWarningFilter(logging.Filter):
    def __init__(self, delay: float = 30.0):
        super().__init__()
        self.delay = delay
        self.last_log_time = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
        if "Invalid HTTP request received" in record.getMessage():
            now = time.monotonic()
            if now - self.last_log_time < self.delay:
                return False
            self.last_log_time = now
        return True


logging.getLogger("uvicorn.error").addFilter(DuplicateHttpWarningFilter(30.0))
log = logging.getLogger("airchat")

# --------------------------------------------------------------------------
# App setup
# --------------------------------------------------------------------------
async def periodic_status_logger():
    while True:
        try:
            await asyncio.sleep(60)
            print(f"[AirChat] Connected users: {manager.connection_count} | Dropped slow clients: {manager.dropped_count}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.debug("Status logger error: %s", e)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    status_task = asyncio.create_task(periodic_status_logger())
    try:
        yield
    finally:
        status_task.cancel()
        try:
            await status_task
        except asyncio.CancelledError:
            pass


for d in ("uploads", "templates", "static/css", "static/js"):
    os.makedirs(d, exist_ok=True)

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# --------------------------------------------------------------------------
# Connection manager
# --------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self._sockets: dict[WebSocket, str] = {}
        self._queues: dict[WebSocket, asyncio.Queue] = {}
        self._tasks: dict[WebSocket, asyncio.Task] = {}
        self._dropped_count: int = 0
        self._users: Counter[str] = Counter()   # supports multiple tabs/devices per name
        self._last_typing: dict[WebSocket, float] = {}
        self._reaction_times: dict[WebSocket, list[float]] = {}

    @property
    def connection_count(self) -> int:
        return len(self._sockets)

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def online(self) -> set[str]:
        return set(self._users)

    async def connect(self, ws: WebSocket, username: str):
        await ws.accept()
        self._sockets[ws] = username
        self._users[username] += 1
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._queues[ws] = queue
        self._tasks[ws] = asyncio.create_task(self._writer_loop(ws, queue))

    async def _writer_loop(self, ws: WebSocket, queue: asyncio.Queue):
        try:
            while True:
                msg = await queue.get()
                try:
                    await asyncio.wait_for(ws.send_json(msg), timeout=5.0)
                except Exception:
                    self._drop_slow_client(ws, reason="send_timeout")
                    break
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            pass
        except Exception:
            self._drop_slow_client(ws, reason="writer_error")

    def _drop_slow_client(self, ws: WebSocket, reason: str = ""):
        if ws not in self._sockets:
            return
        self._dropped_count += 1
        log.warning("Dropping slow/unresponsive client (%s)", reason)
        curr_task = asyncio.current_task()
        task = self._tasks.pop(ws, None)
        if task and task != curr_task and not task.done():
            task.cancel()
        self._queues.pop(ws, None)
        username = self._sockets.pop(ws, None)
        self._last_typing.pop(ws, None)
        self._reaction_times.pop(ws, None)
        if username is not None:
            self._users[username] -= 1
            if self._users[username] <= 0:
                del self._users[username]

        async def _close():
            try:
                await ws.close(code=1001, reason="Slow client")
            except Exception:
                pass

        try:
            asyncio.create_task(_close())
        except RuntimeError:
            pass

    def disconnect(self, ws: WebSocket):
        task = self._tasks.pop(ws, None)
        if task and not task.done():
            task.cancel()
        self._queues.pop(ws, None)
        username = self._sockets.pop(ws, None)
        self._last_typing.pop(ws, None)
        self._reaction_times.pop(ws, None)
        if username is None:
            return
        self._users[username] -= 1
        if self._users[username] <= 0:
            del self._users[username]

    def send_to(self, ws: WebSocket, message: dict):
        queue = self._queues.get(ws)
        if queue is not None:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self._drop_slow_client(ws, reason="queue_full")

    async def broadcast(self, message: dict):
        for ws in list(self._sockets):
            queue = self._queues.get(ws)
            if queue is not None:
                try:
                    queue.put_nowait(message)
                except asyncio.QueueFull:
                    self._drop_slow_client(ws, reason="queue_full")

    async def broadcast_except(self, sender_ws: WebSocket, message: dict):
        for ws in list(self._sockets):
            if ws != sender_ws:
                queue = self._queues.get(ws)
                if queue is not None:
                    try:
                        queue.put_nowait(message)
                    except asyncio.QueueFull:
                        self._drop_slow_client(ws, reason="queue_full")

    async def broadcast_user_list(self):
        await self.broadcast({"type": "presence", "users": list(self._users)})

    def can_type(self, ws: WebSocket) -> bool:
        now = time.monotonic()
        if now - self._last_typing.get(ws, 0.0) < 2.0:
            return False
        self._last_typing[ws] = now
        return True

    def can_react(self, ws: WebSocket) -> bool:
        now = time.monotonic()
        recent = [t for t in self._reaction_times.get(ws, []) if now - t < 1.0]
        if len(recent) >= 5:
            return False
        recent.append(now)
        self._reaction_times[ws] = recent
        return True


manager = ConnectionManager()
upload_semaphore = asyncio.Semaphore(3)


# --------------------------------------------------------------------------
# Filename sanitisation & helpers
# --------------------------------------------------------------------------
def sanitize_filename(filename: str) -> tuple[str, str]:
    """Sanitise original filename into (stored_name, display_name)."""
    # Basename only (strip both / and \ paths), Unicode NFC, drop control chars
    clean_raw = os.path.basename((filename or "file").replace("\\", "/"))
    clean_raw = unicodedata.normalize("NFC", clean_raw)
    clean_raw = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", clean_raw)
    display_name = clean_raw.strip() or "file"

    # Compound extensions or single extension check
    lower_raw = clean_raw.lower()
    ext = ""
    for ce in COMPOUND_EXTS:
        if lower_raw.endswith(ce):
            ext = ce
            stem = clean_raw[:-len(ce)]
            break
    else:
        stem, raw_ext = os.path.splitext(clean_raw)
        raw_ext_lower = raw_ext.lower()
        if re.match(r"^\.[a-z0-9]{1,10}$", raw_ext_lower):
            ext = raw_ext_lower
        else:
            ext = ""
            stem = clean_raw

    # Keep Unicode letters/digits (\w), replace other chars with _
    clean_stem = re.sub(r"[^\w]+", "_", stem)
    clean_stem = re.sub(r"_+", "_", clean_stem).strip("._")
    clean_stem = clean_stem[:80]
    if not clean_stem:
        clean_stem = "file"

    # Guard Windows reserved names
    if clean_stem.upper() in WINDOWS_RESERVED:
        clean_stem = f"file_{clean_stem}"

    stored_name = f"{clean_stem}_{uuid.uuid4().hex[:8]}{ext}"
    return stored_name, display_name


def derive_original_name(stored_name: str) -> str:
    """Derive clean original-looking filename from a stored filename (history fallback)."""
    lower = stored_name.lower()
    for ce in COMPOUND_EXTS:
        if lower.endswith(ce):
            base = stored_name[:-len(ce)]
            clean_base = re.sub(r"_[0-9a-f]{8}$", "", base)
            return f"{clean_base}{ce}"
    p = Path(stored_name)
    ext = p.suffix
    clean_stem = re.sub(r"_[0-9a-f]{8}$", "", p.stem)
    return f"{clean_stem}{ext}"


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("", include_in_schema=False)
async def get_empty():
    return RedirectResponse(url="/")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/api/config")
async def get_config():
    return {"max_upload_bytes": MAX_UPLOAD_BYTES}


@app.get("/api/file-info/{filename:path}")
async def get_file_info(filename: str):
    safe_path = (UPLOAD_DIR / filename).resolve()
    if safe_path.parent != UPLOAD_DIR.resolve() or not safe_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return {
        "exists": True,
        "size": safe_path.stat().st_size,
        "filename": safe_path.name,
        "original_name": derive_original_name(safe_path.name),
    }


# Silent handlers for common browser probes
@app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/apple-touch-icon.png", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/apple-touch-icon-precomposed.png", methods=["GET", "HEAD"], include_in_schema=False)
async def no_content():
    return Response(status_code=204)


@app.api_route("/uploads/{filename:path}", methods=["GET", "HEAD"])
async def serve_upload(filename: str, request: Request):
    safe_path = (UPLOAD_DIR / filename).resolve()
    if safe_path.parent != UPLOAD_DIR.resolve() or not safe_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    ext = safe_path.suffix.lower()
    download_name = derive_original_name(safe_path.name)

    mime_type, _ = mimetypes.guess_type(str(safe_path))
    if not mime_type:
        mime_type = "application/octet-stream"

    is_inline_safe = (ext in INLINE_SAFE_EXTS) and (request.query_params.get("download") != "1")

    headers = {
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "public, max-age=31536000, immutable",
    }

    if is_inline_safe:
        content_disposition = "inline"
    else:
        # Force download with RFC 5987 / RFC 6266 encoding
        ascii_name = download_name.encode("ascii", "replace").decode("ascii").replace('"', '')
        encoded_name = urllib.parse.quote(download_name, encoding="utf-8")
        content_disposition = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'

    headers["Content-Disposition"] = content_disposition

    return FileResponse(
        safe_path,
        media_type=mime_type,
        headers=headers,
    )


@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    username = username.strip()
    if not USERNAME_RE.match(username):
        await websocket.close(code=1008)            # policy violation
        return

    # Cap total websocket connections at 50: close extras with clear reason "Room is full"
    if manager.connection_count >= 50:
        await websocket.accept()
        await websocket.close(code=1008, reason="Room is full")
        return

    # Parse optional after query param
    after_param = websocket.query_params.get("after")
    after_id = None
    if after_param is not None:
        try:
            val = int(after_param)
            if val >= 0:
                after_id = val
        except ValueError:
            after_id = None

    # No-gap rule: connect socket before querying delta
    await manager.connect(websocket, username)
    try:
        def enrich_file_msg(m: dict) -> dict:
            if m.get("msg_type") == "file":
                if not m.get("original_name"):
                    m["original_name"] = derive_original_name(m.get("content", ""))
                if m.get("size") is None:
                    p = (UPLOAD_DIR / m.get("content", "")).resolve()
                    if p.parent == UPLOAD_DIR.resolve() and p.is_file():
                        m["size"] = p.stat().st_size
            return m

        if after_id is not None:
            mode, delta_msgs, rx_snapshot, truncated = await get_messages_delta(after_id)
            if mode == "delta":
                enriched = [enrich_file_msg(m) for m in delta_msgs]
                history_payload = {
                    "type": "history",
                    "mode": "delta",
                    "messages": enriched,
                    "reactions": rx_snapshot,
                }
                if truncated:
                    history_payload["truncated"] = True
                manager.send_to(websocket, history_payload)
            else:
                # after > max_id (database was reset): fall back to full
                raw_history = await get_history()
                enriched = [enrich_file_msg(dict(m)) for m in raw_history]
                manager.send_to(websocket, {"type": "history", "mode": "full", "messages": enriched})
        else:
            raw_history = await get_history()
            enriched = [enrich_file_msg(dict(m)) for m in raw_history]
            manager.send_to(websocket, {"type": "history", "mode": "full", "messages": enriched})

        await manager.broadcast_user_list()

        while True:
            raw_text = (await websocket.receive_text()).strip()
            if not raw_text:
                continue

            if raw_text.startswith("{"):
                try:
                    data = json.loads(raw_text)
                except Exception:
                    # Malformed JSON frame: ignore without crashing
                    continue

                if not isinstance(data, dict):
                    continue

                msg_type = data.get("type")
                if msg_type == "message":
                    content = str(data.get("content", "")).strip()
                    if not content:
                        continue
                    if len(content) > MAX_MESSAGE_CHARS:
                        content = content[:MAX_MESSAGE_CHARS]
                    msg_id = await save_message(username, content, "text")
                    await manager.broadcast({
                        "type": "message",
                        "id": msg_id,
                        "username": username,
                        "content": content,
                        "msg_type": "text",
                    })
                elif msg_type == "typing":
                    if manager.can_type(websocket):
                        await manager.broadcast_except(websocket, {
                            "type": "typing",
                            "username": username,
                        })
                elif msg_type == "react":
                    mid = data.get("id")
                    emoji = data.get("emoji")
                    if isinstance(mid, int) and isinstance(emoji, str):
                        if manager.can_react(websocket):
                            exists, active = await toggle_reaction(mid, username, emoji, ALLOWED_EMOJIS)
                            if exists:
                                await manager.broadcast({
                                    "type": "reaction",
                                    "id": mid,
                                    "emoji": emoji,
                                    "username": username,
                                    "active": active,
                                })
                else:
                    # Unknown frame type: ignore without crashing
                    continue
            else:
                # Backward compatibility: plain text chat message
                content = raw_text
                if len(content) > MAX_MESSAGE_CHARS:
                    content = content[:MAX_MESSAGE_CHARS]
                msg_id = await save_message(username, content, "text")
                await manager.broadcast({
                    "type": "message",
                    "id": msg_id,
                    "username": username,
                    "content": content,
                    "msg_type": "text",
                })
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WebSocket error for %s", username)
    finally:
        manager.disconnect(websocket)
        await manager.broadcast_user_list()


@app.post("/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    username: str = Form(...),
):
    if upload_semaphore.locked():
        raise HTTPException(status_code=503, detail="Server busy, retrying")

    try:
        await asyncio.wait_for(upload_semaphore.acquire(), timeout=0.01)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="Server busy, retrying")

    try:
        username = username.strip()
        if not USERNAME_RE.match(username):
            raise HTTPException(status_code=400, detail="Invalid username")

        # Fast-reject if Content-Length header is larger than limit + 1 MiB overhead
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit():
            if int(content_length) > MAX_UPLOAD_BYTES + 1024 * 1024:
                raise HTTPException(status_code=413, detail="File too large (max 20 MB)")

        # Check free disk space before accepting
        try:
            _, _, free = shutil.disk_usage(UPLOAD_DIR.resolve())
            if free < 2 * MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=507, detail="Insufficient storage on server")
        except OSError:
            pass

        stored_name, original_name = sanitize_filename(file.filename or "file")
        dest_path = (UPLOAD_DIR / stored_name).resolve()
        if dest_path.parent != UPLOAD_DIR.resolve():
            raise HTTPException(status_code=400, detail="Invalid filename")

        size = 0
        try:
            # Exclusive create mode ("xb") ensures no overwrite
            with open(dest_path, "xb") as f:
                while chunk := await file.read(CHUNK_SIZE):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="File too large (max 20 MB)")
                    await asyncio.to_thread(f.write, chunk)

            if size == 0:
                raise HTTPException(status_code=400, detail="Empty file")
        except BaseException:
            dest_path.unlink(missing_ok=True)
            raise

        msg_id = await save_message(username, stored_name, "file", original_name=original_name, size=size)
        await manager.broadcast({
            "type": "message",
            "id": msg_id,
            "username": username,
            "content": stored_name,
            "msg_type": "file",
            "original_name": original_name,
            "size": size,
        })
        return {
            "filename": stored_name,
            "original_name": original_name,
            "size": size,
            "kind": "file",
        }
    finally:
        upload_semaphore.release()


# --------------------------------------------------------------------------
# Launcher helpers
# --------------------------------------------------------------------------
def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))     # doesn't need to be reachable
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def open_browser(url: str):
    chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
    try:
        if chrome.exists():
            webbrowser.register("chrome", None, webbrowser.BackgroundBrowser(str(chrome)))
            webbrowser.get("chrome").open(url)
        else:
            webbrowser.open(url)
    except Exception as e:
        print(f"Could not launch browser automatically: {e}")


def print_banner(url: str):
    line = "=" * 65
    print(f"\n{line}\n={'':63}=\n={'AIRCHAT LOCAL SERVER IS LIVE':^63}=\n={'':63}=\n{line}\n")
    print("   Connect from ANY device on your Wi-Fi using this address:\n")
    print(f"                 --->   {url}   <---\n")
    print("-" * 65)
    print("   WARNING: DO NOT CLOSE THIS WINDOW!")
    print("   Closing this specific window will shut down the chat room.")
    print("-" * 65 + "\n")

    try:
        print("   Scan this QR code with your phone to connect:\n")
        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        print("-" * 65 + "\n")
    except Exception as e:
        print(f"   Could not generate QR code: {e}")

    print("   [AirChat] If a phone fails to connect, make sure the URL uses http:// (not https://).")
    print("   Some browsers auto-upgrade; if so, type the URL manually with http://.\n")


if __name__ == "__main__":
    import webbrowser
    # Spawn a dedicated, visible console window unless already inside it
    if "--run" not in sys.argv:
        flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--run"], creationflags=flags)
        sys.exit(0)

    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("AirChat Server")

    url = f"http://{get_local_ip()}:{PORT}"
    print_banner(url)

    # Open the browser after the server has had a moment to start
    threading.Timer(1.5, open_browser, args=(url,)).start()

    try:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=PORT,
            reload=False,
            ws_ping_interval=30,
            ws_ping_timeout=60,
            ws_max_size=65536,
            timeout_keep_alive=30,
        )
    except Exception as e:
        print(f"\nSERVER ERROR: {e}")
        input("Press Enter to close...")
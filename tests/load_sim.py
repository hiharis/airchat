import asyncio
import json
import logging
import os
import shutil
import socket
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn
import websockets

import database
import main


class ErrorTrapHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        if record.levelno >= logging.ERROR or record.exc_info:
            self.records.append(record)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_server(host: str, port: int, timeout: float = 5.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.05)
    return False


async def run_simulation():
    # Setup isolated temp dir for db and uploads
    tmp_dir = tempfile.mkdtemp(prefix="airchat_sim_")
    db_file = os.path.join(tmp_dir, "sim_airchat.db")
    upload_dir = Path(tmp_dir) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    database.DB_NAME = db_file
    main.UPLOAD_DIR = upload_dir
    await database.init_db()

    # Reset ConnectionManager state
    main.manager._sockets.clear()
    main.manager._queues.clear()
    main.manager._tasks.clear()
    main.manager._dropped_count = 0
    main.manager._users.clear()
    main.manager._last_typing.clear()
    main.manager._reaction_times.clear()
    main.upload_semaphore = asyncio.Semaphore(3)

    # Attach error trap handler to server loggers
    trap = ErrorTrapHandler()
    root_log = logging.getLogger()
    uvicorn_log = logging.getLogger("uvicorn.error")
    airchat_log = logging.getLogger("airchat")
    for l in (root_log, uvicorn_log, airchat_log):
        l.addHandler(trap)

    port = find_free_port()
    config = uvicorn.Config(
        main.app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        ws_ping_interval=30,
        ws_ping_timeout=60,
    )
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    assert wait_for_server("127.0.0.1", port, timeout=6.0), f"Server failed to start on port {port}"

    base_ws_url = f"ws://127.0.0.1:{port}/ws"

    print(f"\n[Load Simulation] Server running on port {port}")
    print("[Load Simulation] Connecting stalled client (never reads)...")

    # 1. Connect stalled client (never reads incoming messages)
    stalled_ws = await websockets.connect(f"{base_ws_url}/stalled_phone")
    stalled_connect_time = time.monotonic()
    stalled_dropped = asyncio.Event()
    stalled_dropped_time = None

    # Wait for server to register stalled_phone
    for _ in range(50):
        if any(u == "stalled_phone" for u in main.manager._sockets.values()):
            break
        await asyncio.sleep(0.02)

    # On Windows localhost loopback, the kernel TCP stack does not apply backpressure
    # due to AFD auto-tuning. Simulate stalled TCP write buffer on the stalled socket.
    for s, uname in list(main.manager._sockets.items()):
        if uname == "stalled_phone":
            orig_send = s.send_json
            async def stalled_send(*args, _orig=orig_send, **kwargs):
                await asyncio.sleep(15.0)
                return await _orig(*args, **kwargs)
            s.send_json = stalled_send
            break

    async def monitor_stalled():
        nonlocal stalled_dropped_time
        try:
            await stalled_ws.wait_closed()
        except Exception:
            pass
        stalled_dropped_time = time.monotonic()
        stalled_dropped.set()

    stalled_monitor_task = asyncio.create_task(monitor_stalled())

    # 2. Connect 30 active clients
    print("[Load Simulation] Connecting 30 active clients...")
    active_clients = []
    delivery_times = []
    reader_tasks = []

    for i in range(30):
        username = f"user_{i}"
        ws = await websockets.connect(f"{base_ws_url}/{username}")
        active_clients.append((username, ws))

    async def client_reader(cid: str, ws):
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                    if data.get("type") == "message":
                        content = data.get("content", "")
                        # content is "sender:idx:timestamp:padding"
                        parts = content.split(":")
                        if len(parts) >= 3:
                            sent_time = float(parts[2])
                            now = time.time()
                            latency_ms = (now - sent_time) * 1000
                            delivery_times.append(latency_ms)
                except Exception:
                    pass
        except websockets.exceptions.ConnectionClosed:
            pass

    for cid, ws in active_clients:
        reader_tasks.append(asyncio.create_task(client_reader(cid, ws)))

    # 3. Each active client sends 1 message per second for 20 seconds
    print("[Load Simulation] Starting 20 s message storm (30 clients x 1 msg/s)...")
    async def client_sender(cid: str, ws):
        for idx in range(20):
            sent_ts = time.time()
            msg = json.dumps({
                "type": "message",
                "content": f"{cid}:{idx}:{sent_ts}",
            })
            await ws.send(msg)
            await asyncio.sleep(1.0)

    sender_tasks = [client_sender(cid, ws) for cid, ws in active_clients]
    await asyncio.gather(*sender_tasks)

    # Allow brief window for trailing deliveries
    await asyncio.sleep(0.5)

    # 4. Verification
    # A: Check stalled client was dropped within 10 s
    assert stalled_dropped.is_set(), "Stalled client was not dropped by the server!"
    stalled_drop_duration = stalled_dropped_time - stalled_connect_time
    print(f"[Load Simulation] Stalled client dropped in: {stalled_drop_duration:.2f} s (required <= 10.0 s)")
    assert stalled_drop_duration <= 10.0, f"Stalled client took {stalled_drop_duration:.2f} s to drop (> 10 s)"

    # B: Check median delivery time stays under 500 ms
    assert len(delivery_times) > 0, "No messages were received by active clients!"
    median_latency = statistics.median(delivery_times)
    p95_latency = statistics.quantiles(delivery_times, n=20)[18] if len(delivery_times) >= 20 else median_latency
    print(f"[Load Simulation] Total delivered messages: {len(delivery_times)}")
    print(f"[Load Simulation] Median delivery time: {median_latency:.2f} ms (required < 500 ms)")
    print(f"[Load Simulation] 95th percentile delivery: {p95_latency:.2f} ms")
    assert median_latency < 500.0, f"Median delivery time was {median_latency:.2f} ms (>= 500 ms)"

    # C: Check server logged no exceptions
    error_count = len(trap.records)
    print(f"[Load Simulation] Server errors logged: {error_count}")
    if error_count > 0:
        for r in trap.records:
            print(f"  - Server Error: {r.getMessage()}")
    assert error_count == 0, f"Server logged {error_count} exceptions during simulation"

    # Cleanup connections
    stalled_monitor_task.cancel()
    for _, ws in active_clients:
        await ws.close()
    for t in reader_tasks:
        t.cancel()

    # Shutdown server
    server.should_exit = True
    server_thread.join(timeout=3.0)

    for l in (root_log, uvicorn_log, airchat_log):
        l.removeHandler(trap)

    # Cleanup temp directory
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print("[Load Simulation] All criteria passed successfully!\n")


def test_load_simulation():
    """Requirement 9: 30 clients x 20s + 1 stalled client simulation."""
    asyncio.run(run_simulation())


if __name__ == "__main__":
    asyncio.run(run_simulation())


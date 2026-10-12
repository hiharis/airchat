import asyncio
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

# Ensure project root in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import uvicorn
import websockets
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import database
import main


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def isolated_env():
    tmp_dir = tempfile.mkdtemp(prefix="airchat_presence_test_")
    db_file = os.path.join(tmp_dir, "test_airchat.db")
    upload_dir = Path(tmp_dir) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    old_db = database.DB_NAME
    old_upload = main.UPLOAD_DIR

    database.DB_NAME = db_file
    main.UPLOAD_DIR = upload_dir

    main.manager._sockets.clear()
    main.manager._queues.clear()
    main.manager._tasks.clear()
    main.manager._dropped_count = 0
    main.manager._users.clear()
    main.manager._last_typing.clear()
    main.manager._reaction_times.clear()
    main.upload_semaphore = asyncio.Semaphore(3)

    asyncio.run(database.init_db())

    yield

    database.DB_NAME = old_db
    main.UPLOAD_DIR = old_upload
    shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# 1. Presence Tests
# --------------------------------------------------------------------------
def test_presence_two_tabs_same_user_and_disconnect():
    """Two tabs with one username stay online until the last tab closes."""
    with TestClient(main.app) as client:
        # Tab 1 connects
        with client.websocket_connect("/ws/alice") as ws1:
            ws1.receive_json()  # history
            pres1 = ws1.receive_json()  # presence
            assert "alice" in pres1.get("users", [])
            assert main.manager._users["alice"] == 1

            # Tab 2 connects with same username
            with client.websocket_connect("/ws/alice") as ws2:
                ws2.receive_json()  # history
                pres2 = ws2.receive_json()
                assert "alice" in pres2.get("users", [])
                assert main.manager._users["alice"] == 2

            # Tab 2 closed: alice must still be online in manager and presence
            assert "alice" in main.manager.online
            assert main.manager._users["alice"] == 1

        # Tab 1 closed: alice is now fully offline
        assert "alice" not in main.manager.online
        assert main.manager._users["alice"] == 0


def test_invalid_username_rejected_with_1008():
    """Invalid usernames (containing invalid characters or empty) are rejected with code 1008."""
    with TestClient(main.app) as client:
        # Invalid username containing slash or bad chars
        with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect("/ws/bad$user"):
            pass
        assert exc_info.value.code == 1008

        # Username with whitespace only or > 32 chars
        with pytest.raises(WebSocketDisconnect) as exc_info2, client.websocket_connect("/ws/" + ("a" * 35)):
            pass
        assert exc_info2.value.code == 1008


def test_message_length_capped_at_max_chars():
    """Chat messages longer than MAX_MESSAGE_CHARS are capped without crashing."""
    with TestClient(main.app) as client, client.websocket_connect("/ws/alice") as ws_alice:
        ws_alice.receive_json()  # history
        ws_alice.receive_json()  # presence

        long_msg = "A" * (main.MAX_MESSAGE_CHARS + 500)
        ws_alice.send_json({"type": "message", "content": long_msg})

        broadcast = ws_alice.receive_json()
        assert broadcast["type"] == "message"
        assert len(broadcast["content"]) == main.MAX_MESSAGE_CHARS
        assert broadcast["content"] == "A" * main.MAX_MESSAGE_CHARS


# --------------------------------------------------------------------------
# 2. Database Migration Tests
# --------------------------------------------------------------------------
def test_database_old_schema_migration_preserves_data():
    """Migrating an old-style database schema (lacking original_name, size, reactions) preserves all data."""
    tmp_dir = tempfile.mkdtemp(prefix="airchat_mig_test_")
    mig_db_file = os.path.join(tmp_dir, "old_airchat.db")
    database.DB_NAME = mig_db_file

    import aiosqlite

    async def create_old_db():
        async with aiosqlite.connect(mig_db_file) as db:
            # Old schema without original_name, size, and without reactions table
            await db.execute("""
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    content TEXT,
                    type TEXT NOT NULL, 
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute(
                "INSERT INTO messages (username, content, type) VALUES (?, ?, ?)",
                ("old_user", "old message content 1", "text"),
            )
            await db.execute(
                "INSERT INTO messages (username, content, type) VALUES (?, ?, ?)",
                ("old_user", "old message content 2", "text"),
            )
            await db.commit()

    asyncio.run(create_old_db())

    # Now run init_db() which runs the migration
    asyncio.run(database.init_db())

    # Verify old data preserved
    history = asyncio.run(database.get_history())
    assert len(history) == 2
    assert history[0]["username"] == "old_user"
    assert history[0]["content"] == "old message content 1"
    assert history[1]["content"] == "old message content 2"
    assert history[0]["id"] == 1
    assert history[1]["id"] == 2

    # Save a new message with metadata and reactions
    new_id = asyncio.run(database.save_message("new_user", "new message", "file", "doc.pdf", 1234))
    assert new_id == 3

    # Toggle reaction on old and new messages
    exists1, active1 = asyncio.run(database.toggle_reaction(1, "alice", "👍", main.ALLOWED_EMOJIS))
    assert exists1 and active1
    exists2, active2 = asyncio.run(database.toggle_reaction(3, "bob", "🔥", main.ALLOWED_EMOJIS))
    assert exists2 and active2

    # Verify calling init_db again is completely idempotent
    asyncio.run(database.init_db())
    history2 = asyncio.run(database.get_history())
    assert len(history2) == 3
    assert history2[0]["reactions"]["👍"] == ["alice"]
    assert history2[2]["reactions"]["🔥"] == ["bob"]

    shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# 3. Real Server Integration Test
# --------------------------------------------------------------------------
def test_real_server_websockets_integration():
    """Start real uvicorn server in a thread and verify real websockets connection, broadcast, and disconnect."""
    port = find_free_port()
    config = uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()

    # Wait for server ready
    ready = False
    start = time.time()
    while time.time() - start < 5.0:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                ready = True
                break
        except (OSError, ConnectionRefusedError):
            time.sleep(0.05)
    assert ready, "Real uvicorn server failed to boot"

    async def client_interaction():
        url = f"ws://127.0.0.1:{port}/ws/charlie"
        async with websockets.connect(url) as ws:
            raw_hist = await ws.recv()
            assert "history" in raw_hist

            raw_pres = await ws.recv()
            assert "presence" in raw_pres
            assert "charlie" in raw_pres

            # Send a chat message
            await ws.send('{"type":"message","content":"hello from real websockets"}')
            raw_msg = await ws.recv()
            assert "hello from real websockets" in raw_msg

        # Client disconnected: verify manager cleaned it up
        await asyncio.sleep(0.2)
        assert "charlie" not in main.manager.online

    asyncio.run(client_interaction())

    server.should_exit = True
    th.join(timeout=3.0)

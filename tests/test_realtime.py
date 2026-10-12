import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

import database
import main


@pytest.fixture(autouse=True)
def setup_isolated_env(tmp_path, monkeypatch):
    """Isolate uploads directory, database, and connection manager for every test."""
    test_upload_dir = tmp_path / "uploads"
    test_upload_dir.mkdir(parents=True, exist_ok=True)
    test_db_file = str(tmp_path / "test_airchat.db")

    monkeypatch.setattr(main, "UPLOAD_DIR", test_upload_dir)
    monkeypatch.setattr(database, "DB_NAME", test_db_file)

    # Reset ConnectionManager state
    monkeypatch.setattr(main.manager, "_sockets", {})
    monkeypatch.setattr(main.manager, "_queues", {})
    monkeypatch.setattr(main.manager, "_tasks", {})
    monkeypatch.setattr(main.manager, "_dropped_count", 0)
    monkeypatch.setattr(main.manager, "_users", Counter())
    monkeypatch.setattr(main.manager, "_last_typing", {})
    monkeypatch.setattr(main.manager, "_reaction_times", {})
    monkeypatch.setattr(main, "upload_semaphore", asyncio.Semaphore(3))

    asyncio.run(database.init_db())
    yield


def drain_initial_messages(ws):
    """Drain initial history and presence messages sent upon connection."""
    # First frame is always history
    hist = ws.receive_json()
    # Second frame is presence
    pres = ws.receive_json()
    return hist, pres


# --------------------------------------------------------------------------
# Test 1: Saved messages get strictly increasing IDs; live broadcasts & history include id
# --------------------------------------------------------------------------
def test_1_saved_messages_sequential_ids_and_broadcast():
    """1. Saved messages get strictly increasing ids; live broadcasts and history include id."""
    async def run_db_saves():
        id1 = await database.save_message("alice", "first msg", "text")
        id2 = await database.save_message("bob", "second msg", "text")
        return id1, id2

    id1, id2 = asyncio.run(run_db_saves())
    assert isinstance(id1, int)
    assert isinstance(id2, int)
    assert id2 > id1

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            hist, _ = drain_initial_messages(ws)
            assert hist["type"] == "history"
            msgs = hist["messages"]
            assert len(msgs) == 2
            assert msgs[0]["id"] == id1
            assert msgs[1]["id"] == id2
            assert msgs[0]["content"] == "first msg"
            assert msgs[1]["content"] == "second msg"

            # Send a live message
            ws.send_json({"type": "message", "content": "third msg"})
            live = ws.receive_json()
            assert live["type"] == "message"
            assert live["username"] == "alice"
            assert live["content"] == "third msg"
            assert live["id"] > id2


# --------------------------------------------------------------------------
# Test 2: Plain-text frame works; JSON message frame works; malformed JSON & unknown types ignored
# --------------------------------------------------------------------------
def test_2_plaintext_json_and_malformed_frames():
    """2. Plain-text frame still works as a message; JSON message frame works; malformed JSON and unknown types are ignored without crashing."""
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws/alice") as ws_alice:
            drain_initial_messages(ws_alice)

            with client.websocket_connect("/ws/bob") as ws_bob:
                # ws_alice receives bob's presence update
                ws_alice.receive_json()
                drain_initial_messages(ws_bob)

                # A: Plain-text frame works
                ws_alice.send_text("hello plain text")
                msg_alice = ws_alice.receive_json()
                msg_bob = ws_bob.receive_json()
                assert msg_alice["type"] == "message"
                assert msg_alice["content"] == "hello plain text"
                assert msg_alice["username"] == "alice"
                assert "id" in msg_alice
                assert msg_bob["content"] == "hello plain text"

                # B: JSON message frame works
                ws_bob.send_text(json.dumps({"type": "message", "content": "hello json"}))
                msg_a = ws_alice.receive_json()
                msg_b = ws_bob.receive_json()
                assert msg_a["content"] == "hello json"
                assert msg_b["content"] == "hello json"
                assert msg_b["username"] == "bob"

                # C: Malformed JSON frame ignored without crashing
                ws_alice.send_text("{bad json, not closed...")
                # D: Unknown frame type ignored without crashing
                ws_alice.send_text(json.dumps({"type": "unknown_action", "foo": "bar"}))

                # E: Send another valid message to prove sockets are alive and no crash occurred
                ws_alice.send_text(json.dumps({"type": "message", "content": "still alive"}))
                alive_a = ws_alice.receive_json()
                alive_b = ws_bob.receive_json()
                assert alive_a["content"] == "still alive"
                assert alive_b["content"] == "still alive"


# --------------------------------------------------------------------------
# Test 3: Delta reconnect: ?after=N, missing/invalid, after > max_id, >200 missed, empty delta
# --------------------------------------------------------------------------
def test_3_delta_reconnect_after_param():
    """3. ?after=N returns only ids > N, oldest first, mode:'delta'; after absent -> mode:'full'; after > max id -> mode:'full'; more than 200 missed -> newest 200 + truncated:true; nothing missed -> empty delta."""
    # Seed 5 messages
    async def seed_messages(count):
        ids = []
        for i in range(count):
            mid = await database.save_message("alice", f"msg {i+1}", "text")
            ids.append(mid)
        return ids

    ids = asyncio.run(seed_messages(5))
    max_id = ids[-1]

    with TestClient(main.app) as client:
        # A: after absent -> mode: "full"
        with client.websocket_connect("/ws/alice") as ws:
            hist, _ = drain_initial_messages(ws)
            assert hist["type"] == "history"
            assert hist["mode"] == "full"
            assert len(hist["messages"]) == 5

        # B: after invalid string -> mode: "full"
        with client.websocket_connect("/ws/alice?after=invalid") as ws:
            hist, _ = drain_initial_messages(ws)
            assert hist["mode"] == "full"
            assert len(hist["messages"]) == 5

        # C: after=3 -> mode: "delta", returns ids [4, 5], oldest first
        with client.websocket_connect(f"/ws/alice?after={ids[2]}") as ws:
            hist, _ = drain_initial_messages(ws)
            assert hist["mode"] == "delta"
            assert [m["id"] for m in hist["messages"]] == [ids[3], ids[4]]

        # D: nothing missed (after=max_id) -> mode: "delta", empty list
        with client.websocket_connect(f"/ws/alice?after={max_id}") as ws:
            hist, _ = drain_initial_messages(ws)
            assert hist["mode"] == "delta"
            assert hist["messages"] == []

        # E: after > max_id (database was reset) -> mode: "full"
        with client.websocket_connect(f"/ws/alice?after={max_id + 999}") as ws:
            hist, _ = drain_initial_messages(ws)
            assert hist["mode"] == "full"
            assert len(hist["messages"]) == 5

        # F: More than 200 missed -> capped at newest 200 + truncated: true
        bulk_ids = asyncio.run(seed_messages(210))
        with client.websocket_connect("/ws/alice?after=0") as ws:
            hist, _ = drain_initial_messages(ws)
            assert hist["mode"] == "delta"
            assert hist["truncated"] is True
            assert len(hist["messages"]) == 200
            # Ensure sorted oldest first of newest 200
            m_ids = [m["id"] for m in hist["messages"]]
            assert m_ids == sorted(m_ids)
            assert m_ids[-1] == bulk_ids[-1]


# --------------------------------------------------------------------------
# Test 4: Message sent during connect is not lost (no-gap rule) and deduplication
# --------------------------------------------------------------------------
def test_4_no_gap_rule_and_duplicate_handling():
    """4. A message sent while a client is connecting is not lost (no-gap rule) and duplicates are harmless."""
    with TestClient(main.app) as client:
        # Pre-seed 1 message
        asyncio.run(database.save_message("alice", "msg 1", "text"))

        with client.websocket_connect("/ws/alice") as ws_alice:
            drain_initial_messages(ws_alice)

            # When bob connects, manager.connect registers ws_bob before get_messages_delta
            with client.websocket_connect("/ws/bob?after=0") as ws_bob:
                # ws_alice gets bob's presence
                ws_alice.receive_json()

                # If alice sends a message immediately, bob's socket receives the broadcast
                ws_alice.send_json({"type": "message", "content": "concurrent message"})

                # bob receives delta history first
                hist = ws_bob.receive_json()
                assert hist["type"] == "history"

                # bob receives presence
                pres = ws_bob.receive_json()
                assert pres["type"] == "presence"

                # bob receives alice's concurrent broadcast
                broadcast = ws_bob.receive_json()
                assert broadcast["type"] == "message"
                assert broadcast["content"] == "concurrent message"

                # Deduplication check: broadcast has valid id and can be deduped
                assert isinstance(broadcast["id"], int)


# --------------------------------------------------------------------------
# Test 5: Typing broadcast to others only, not sender, never stored, rate-limited
# --------------------------------------------------------------------------
def test_5_typing_indicator_broadcast_and_rate_limit():
    """5. typing is broadcast to others only, never to the sender, never stored, and rate limited (two events 0.5 s apart -> one broadcast)."""
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws/alice") as ws_alice:
            drain_initial_messages(ws_alice)

            with client.websocket_connect("/ws/bob") as ws_bob:
                ws_alice.receive_json()  # bob presence
                drain_initial_messages(ws_bob)

                # 1. Alice sends typing (event 1)
                ws_alice.send_json({"type": "typing"})

                # Bob receives typing
                typing_msg = ws_bob.receive_json()
                assert typing_msg["type"] == "typing"
                assert typing_msg["username"] == "alice"

                # Alice receives NOTHING from typing (typing is never sent to sender)
                # Rate limit check: Alice sends typing event 2 immediately (< 2s cooldown)
                ws_alice.send_json({"type": "typing"})

                # Alice immediately sends a message
                ws_alice.send_json({"type": "message", "content": "ping"})

                # Alice receives message directly
                msg_a = ws_alice.receive_json()
                assert msg_a["type"] == "message"
                assert msg_a["content"] == "ping"

                # Bob receives the message directly, NOT a second typing event
                msg_b = ws_bob.receive_json()
                assert msg_b["type"] == "message"
                assert msg_b["content"] == "ping"

    # 2. Check typing was never stored in the database
    async def check_typing_in_db():
        history = await database.get_history()
        return [m for m in history if m["content"] == "typing" or m.get("msg_type") == "typing"]

    assert asyncio.run(check_typing_in_db()) == []


# --------------------------------------------------------------------------
# Test 6: Reactions toggle on/off, whitelist, reject invalid id, cap 12 emojis, rate-limit
# --------------------------------------------------------------------------
def test_6_reactions_toggle_whitelist_and_cap():
    """6. react toggles on and off, rejects a non-whitelisted emoji and a non-existent id, caps 12 emojis per message, and rate-limits spam."""
    mid = asyncio.run(database.save_message("alice", "react target", "text"))

    with TestClient(main.app) as client:
        with client.websocket_connect("/ws/alice") as ws_alice:
            drain_initial_messages(ws_alice)

            with client.websocket_connect("/ws/bob") as ws_bob:
                ws_alice.receive_json()  # bob presence
                drain_initial_messages(ws_bob)

                # A: Toggle on
                ws_alice.send_json({"type": "react", "id": mid, "emoji": "👍"})
                rx_on_a = ws_alice.receive_json()
                rx_on_b = ws_bob.receive_json()
                assert rx_on_a == {"type": "reaction", "id": mid, "emoji": "👍", "username": "alice", "active": True}
                assert rx_on_b == rx_on_a

                # B: Toggle off (same user + emoji)
                ws_alice.send_json({"type": "react", "id": mid, "emoji": "👍"})
                rx_off_a = ws_alice.receive_json()
                rx_off_b = ws_bob.receive_json()
                assert rx_off_a == {"type": "reaction", "id": mid, "emoji": "👍", "username": "alice", "active": False}
                assert rx_off_b == rx_off_a

                # C: Reject non-whitelisted emoji
                ws_alice.send_json({"type": "react", "id": mid, "emoji": "🚀"})
                # D: Reject non-existent message ID
                ws_alice.send_json({"type": "react", "id": 999999, "emoji": "❤️"})

                # Send a normal message to verify no reaction was broadcast and connection is alive
                ws_alice.send_json({"type": "message", "content": "verify rx ignored"})
                msg_a = ws_alice.receive_json()
                msg_b = ws_bob.receive_json()
                assert msg_a["content"] == "verify rx ignored"
                assert msg_b["content"] == "verify rx ignored"

                # E: Rate-limit spam (max 5 reactions per second)
                # Wait 1.1s so previous test reactions fall outside the 1.0s window
                time.sleep(1.1)

                # Alice sends 7 reactions rapidly
                for _ in range(7):
                    ws_alice.send_json({"type": "react", "id": mid, "emoji": "🔥"})

                # Alice immediately sends a message
                ws_alice.send_json({"type": "message", "content": "after spam"})

                # Collect frames until message "after spam" is reached
                received_rx_count = 0
                while True:
                    frame = ws_bob.receive_json()
                    if frame.get("type") == "reaction":
                        received_rx_count += 1
                    elif frame.get("type") == "message" and frame.get("content") == "after spam":
                        break

                # Exactly 5 reactions made it through before rate limit kicked in
                assert received_rx_count == 5

    # F: Cap 12 distinct emojis per message
    async def test_cap_12():
        mid_cap = await database.save_message("alice", "target for cap", "text")
        test_emojis = [f"e{i}" for i in range(15)]
        allowed = set(test_emojis)
        # Add 12 distinct emojis
        for emo in test_emojis[:12]:
            exists, active = await database.toggle_reaction(mid_cap, f"user_{emo}", emo, allowed)
            assert exists is True
            assert active is True

        # 13th distinct emoji should be rejected
        exists, active = await database.toggle_reaction(mid_cap, "user_13", test_emojis[12], allowed)
        assert exists is True
        assert active is False

    asyncio.run(test_cap_12())


# --------------------------------------------------------------------------
# Test 7: Reactions map attached to history/delta; DB migration idempotent
# --------------------------------------------------------------------------
def test_7_reactions_attached_to_history_and_migration_idempotent():
    """7. History and delta carry correct reactions maps; the reactions table creation is idempotent and works on an existing DB."""
    # Pre-seed message with reactions
    async def seed():
        mid = await database.save_message("alice", "message with reactions", "text")
        await database.toggle_reaction(mid, "bob", "👍", main.ALLOWED_EMOJIS)
        await database.toggle_reaction(mid, "carol", "👍", main.ALLOWED_EMOJIS)
        await database.toggle_reaction(mid, "dave", "❤️", main.ALLOWED_EMOJIS)
        return mid

    mid = asyncio.run(seed())

    with TestClient(main.app) as client:
        # Full history includes reactions
        with client.websocket_connect("/ws/alice") as ws:
            hist, _ = drain_initial_messages(ws)
            assert hist["type"] == "history"
            assert hist["mode"] == "full"
            msg = hist["messages"][0]
            assert msg["id"] == mid
            assert msg["reactions"] == {
                "👍": ["bob", "carol"],
                "❤️": ["dave"],
            }

        # Delta reconnect includes reactions on messages and top-level reactions snapshot
        with client.websocket_connect("/ws/alice?after=0") as ws:
            delta, _ = drain_initial_messages(ws)
            assert delta["mode"] == "delta"
            assert delta["messages"][0]["reactions"]["👍"] == ["bob", "carol"]
            assert str(mid) in [str(k) for k in delta["reactions"].keys()]

    # Test idempotence: calling init_db multiple times does not drop or fail
    async def rerun_migration():
        await database.init_db()
        await database.init_db()
        history = await database.get_history()
        assert len(history) >= 1
        assert history[0]["reactions"]["👍"] == ["bob", "carol"]

    asyncio.run(rerun_migration())


# --------------------------------------------------------------------------
# Test 8: /uploads/<file> Cache-Control immutable, nosniff, attachment for HTML/SVG
# --------------------------------------------------------------------------
def test_8_uploads_cache_headers_and_disposition():
    """8. /uploads/<file> has Cache-Control … immutable and still has nosniff and the attachment rule for HTML/SVG."""
    # Create test files in UPLOAD_DIR
    png_file = main.UPLOAD_DIR / "sample_img_12345678.png"
    png_file.write_bytes(b"\x89PNG\r\n\x1a\nfakeimagebytes")

    svg_file = main.UPLOAD_DIR / "graphic_12345678.svg"
    svg_file.write_text("<svg>evil</svg>", encoding="utf-8")

    html_file = main.UPLOAD_DIR / "document_12345678.html"
    html_file.write_text("<html>hello</html>", encoding="utf-8")

    with TestClient(main.app) as client:
        # Safe image -> inline + immutable + nosniff
        res_png = client.get(f"/uploads/{png_file.name}")
        assert res_png.status_code == 200
        assert "public, max-age=31536000, immutable" in res_png.headers["cache-control"]
        assert res_png.headers["x-content-type-options"] == "nosniff"
        assert res_png.headers["content-disposition"] == "inline"

        # SVG -> attachment + immutable + nosniff
        res_svg = client.get(f"/uploads/{svg_file.name}")
        assert res_svg.status_code == 200
        assert "public, max-age=31536000, immutable" in res_svg.headers["cache-control"]
        assert res_svg.headers["x-content-type-options"] == "nosniff"
        assert res_svg.headers["content-disposition"].startswith("attachment;")

        # HTML -> attachment + immutable + nosniff
        res_html = client.get(f"/uploads/{html_file.name}")
        assert res_html.status_code == 200
        assert "public, max-age=31536000, immutable" in res_html.headers["cache-control"]
        assert res_html.headers["x-content-type-options"] == "nosniff"
        assert res_html.headers["content-disposition"].startswith("attachment;")


# --------------------------------------------------------------------------
# Test 9: Cap total websocket connections at 50 with "Room is full"
# --------------------------------------------------------------------------
def test_9_websocket_cap_at_50():
    """3. Cap total websocket connections at 50: close extras with clear reason 'Room is full'."""
    with TestClient(main.app) as client:
        # Simulate 50 active sockets
        for i in range(50):
            main.manager._sockets[f"fake_sock_{i}"] = f"user_{i}"

        from starlette.websockets import WebSocketDisconnect
        with client.websocket_connect("/ws/extra_user") as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            assert exc_info.value.code == 1008
            assert exc_info.value.reason == "Room is full"


# --------------------------------------------------------------------------
# Test 10: Limit concurrent uploads with Semaphore(3) returning 503
# --------------------------------------------------------------------------
def test_10_upload_semaphore_limit_returns_503():
    """4. Limit concurrent uploads with asyncio.Semaphore(3). When busy return 503 'Server busy, retrying'."""
    main.upload_semaphore._value = 0
    with TestClient(main.app) as client:
        files = {"file": ("busy.txt", b"busy test content", "text/plain")}
        res = client.post("/upload", files=files, data={"username": "alice"})
        assert res.status_code == 503
        assert "Server busy, retrying" in res.json().get("detail", "")

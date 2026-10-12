import asyncio
import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

import database
import main


@pytest.fixture(autouse=True)
def setup_isolated_env(tmp_path, monkeypatch):
    """Isolate uploads directory and database for every test."""
    test_upload_dir = tmp_path / "uploads"
    test_upload_dir.mkdir(parents=True, exist_ok=True)
    test_db_file = str(tmp_path / "test_airchat.db")

    monkeypatch.setattr(main, "UPLOAD_DIR", test_upload_dir)
    monkeypatch.setattr(database, "DB_NAME", test_db_file)
    monkeypatch.setattr(main, "upload_semaphore", asyncio.Semaphore(3))
    monkeypatch.setattr(main.manager, "_sockets", {})
    monkeypatch.setattr(main.manager, "_queues", {})
    monkeypatch.setattr(main.manager, "_tasks", {})
    monkeypatch.setattr(main.manager, "_dropped_count", 0)

    # Initialize the test DB
    asyncio.run(database.init_db())

    yield

    # Cleanup is handled automatically by tmp_path


def test_1_kb_txt_upload():
    """1. 1 KB .txt -> 200, appears in broadcast with msg_type:'file'."""
    broadcasts = []

    async def mock_broadcast(msg):
        broadcasts.append(msg)

    with TestClient(main.app) as client:
        main.manager.broadcast = mock_broadcast

        content = b"a" * 1024
        files = {"file": ("test.txt", content, "text/plain")}
        data = {"username": "alice"}
        response = client.post("/upload", files=files, data=data)

        assert response.status_code == 200
        res_data = response.json()
        assert res_data["original_name"] == "test.txt"
        assert res_data["size"] == 1024
        assert res_data["filename"].endswith(".txt")
        assert (main.UPLOAD_DIR / res_data["filename"]).is_file()

        assert len(broadcasts) == 1
        b_msg = broadcasts[0]
        assert b_msg["type"] == "message"
        assert b_msg["username"] == "alice"
        assert b_msg["msg_type"] == "file"
        assert b_msg["original_name"] == "test.txt"
        assert b_msg["size"] == 1024


def test_various_extensions_and_compound():
    """2. .py, .zip, .docx, no-extension file, .tar.gz -> all 200, extension preserved (.tar.gz kept whole)."""
    test_files = [
        ("script.py", b"print('hello')", ".py"),
        ("archive.zip", b"PK\x03\x04fakezip", ".zip"),
        ("document.docx", b"PK\x03\x04fakedocx", ".docx"),
        ("README", b"no extension content", ""),
        ("bundle.tar.gz", b"\x1f\x8b\x08faketargz", ".tar.gz"),
    ]

    with TestClient(main.app) as client:
        for fname, content, expected_ext in test_files:
            files = {"file": (fname, content, "application/octet-stream")}
            data = {"username": "bob"}
            res = client.post("/upload", files=files, data=data)
            assert res.status_code == 200, f"Failed for {fname}"
            stored = res.json()["filename"]
            assert stored.endswith(expected_ext), f"Extension mismatch for {fname}: got {stored}"
            assert res.json()["original_name"] == fname


def test_size_limits_exact_and_over():
    """3. Exactly 20 * 1024 * 1024 bytes -> 200. 20 * 1024 * 1024 + 1 bytes -> 413, and no file left in uploads/."""
    exact_limit = 20 * 1024 * 1024

    with TestClient(main.app) as client:
        # Exactly 20 MB
        chunk = b"x" * (1024 * 1024)
        # Generator for streaming 20 MB without large memory overhead
        def exact_stream():
            for _ in range(20):
                yield chunk

        res_exact = client.post(
            "/upload",
            files={"file": ("exact20mb.bin", b"".join(exact_stream()), "application/octet-stream")},
            data={"username": "tester"},
        )
        assert res_exact.status_code == 200
        stored_name = res_exact.json()["filename"]
        assert (main.UPLOAD_DIR / stored_name).is_file()
        assert (main.UPLOAD_DIR / stored_name).stat().st_size == exact_limit

        # 20 MB + 1 byte
        def over_stream():
            for _ in range(20):
                yield chunk
            yield b"!"

        files_before = set(os.listdir(main.UPLOAD_DIR))

        res_over = client.post(
            "/upload",
            files={"file": ("over20mb.bin", b"".join(over_stream()), "application/octet-stream")},
            data={"username": "tester"},
        )
        assert res_over.status_code == 413
        assert "File too large" in res_over.json()["detail"]

        files_after = set(os.listdir(main.UPLOAD_DIR))
        # Ensure no extra file is left in uploads/
        assert files_after == files_before


def test_empty_file_rejected():
    """4. 0-byte file -> 400."""
    with TestClient(main.app) as client:
        files = {"file": ("empty.txt", b"", "text/plain")}
        res = client.post("/upload", files=files, data={"username": "tester"})
        assert res.status_code == 400
        assert "Empty file" in res.json()["detail"]
        assert len(os.listdir(main.UPLOAD_DIR)) == 0


def test_lying_content_length():
    """5. Lying Content-Length (small header, large body) -> still rejected at 20 MB and cleaned up."""
    # When client sends small content-length header or chunks that exceed 20 MB
    with TestClient(main.app) as client:
        files_before = set(os.listdir(main.UPLOAD_DIR))
        # 21 MB payload
        big_content = b"0" * (21 * 1024 * 1024)
        # Pass headers with small Content-Length override
        res = client.post(
            "/upload",
            files={"file": ("big.dat", big_content, "application/octet-stream")},
            data={"username": "tester"},
            headers={"Content-Length": "100"},
        )
        assert res.status_code == 413
        files_after = set(os.listdir(main.UPLOAD_DIR))
        assert files_after == files_before


def test_filename_sanitization_and_traversal():
    """6. Filenames with traversal, Windows reserved names, long names, special characters -> safe inside uploads/."""
    weird_names = [
        "../../evil.py",
        r"..\..\evil.exe",
        "CON.txt",
        "   .hidden",
        "a" * 300 + ".txt",
        "ملف_تجريبي.pdf",
        "🎉party🚀.zip",
    ]

    with TestClient(main.app) as client:
        for fname in weird_names:
            res = client.post(
                "/upload",
                files={"file": (fname, b"test payload", "application/octet-stream")},
                data={"username": "tester"},
            )
            assert res.status_code == 200, f"Failed for {fname}"
            stored = res.json()["filename"]
            target_path = (main.UPLOAD_DIR / stored).resolve()
            assert target_path.parent == main.UPLOAD_DIR.resolve()
            assert target_path.is_file()
            assert not stored.startswith("..")
            assert not stored.startswith("/")


def test_duplicate_filenames_no_overwrite():
    """7. Two uploads with the same filename -> two different stored files, nothing overwritten."""
    with TestClient(main.app) as client:
        res1 = client.post(
            "/upload",
            files={"file": ("same.txt", b"version 1", "text/plain")},
            data={"username": "user1"},
        )
        assert res1.status_code == 200
        file1 = res1.json()["filename"]

        res2 = client.post(
            "/upload",
            files={"file": ("same.txt", b"version 2", "text/plain")},
            data={"username": "user2"},
        )
        assert res2.status_code == 200
        file2 = res2.json()["filename"]

        assert file1 != file2
        assert (main.UPLOAD_DIR / file1).read_bytes() == b"version 1"
        assert (main.UPLOAD_DIR / file2).read_bytes() == b"version 2"


def test_safe_file_serving_headers():
    """8. GET /uploads/<x>.html and .svg -> Content-Disposition: attachment and nosniff. GET /uploads/<x>.png -> inline. ?download=1 on a PDF -> attachment."""
    with TestClient(main.app) as client:
        # 1. HTML file
        res_html = client.post("/upload", files={"file": ("xss.html", b"<h1>evil</h1>", "text/html")}, data={"username": "u"})
        html_stored = res_html.json()["filename"]
        get_html = client.get(f"/uploads/{html_stored}")
        assert get_html.status_code == 200
        assert get_html.headers.get("X-Content-Type-Options") == "nosniff"
        assert "attachment" in get_html.headers.get("Content-Disposition", "")

        # 2. SVG file
        res_svg = client.post("/upload", files={"file": ("vector.svg", b"<svg></svg>", "image/svg+xml")}, data={"username": "u"})
        svg_stored = res_svg.json()["filename"]
        get_svg = client.get(f"/uploads/{svg_stored}")
        assert get_svg.status_code == 200
        assert get_svg.headers.get("X-Content-Type-Options") == "nosniff"
        assert "attachment" in get_svg.headers.get("Content-Disposition", "")

        # 3. PNG file (inline)
        res_png = client.post("/upload", files={"file": ("pic.png", b"\x89PNGfake", "image/png")}, data={"username": "u"})
        png_stored = res_png.json()["filename"]
        get_png = client.get(f"/uploads/{png_stored}")
        assert get_png.status_code == 200
        assert get_png.headers.get("X-Content-Type-Options") == "nosniff"
        assert get_png.headers.get("Content-Disposition") == "inline"

        # 4. PDF with ?download=1
        res_pdf = client.post("/upload", files={"file": ("doc.pdf", b"%PDFfake", "application/pdf")}, data={"username": "u"})
        pdf_stored = res_pdf.json()["filename"]
        get_pdf_inline = client.get(f"/uploads/{pdf_stored}")
        assert get_pdf_inline.headers.get("Content-Disposition") == "inline"

        get_pdf_dl = client.get(f"/uploads/{pdf_stored}?download=1")
        assert "attachment" in get_pdf_dl.headers.get("Content-Disposition", "")


def test_invalid_username_rejected():
    """9. Invalid username on /upload -> 400."""
    with TestClient(main.app) as client:
        res = client.post(
            "/upload",
            files={"file": ("valid.txt", b"hello", "text/plain")},
            data={"username": "   "},
        )
        assert res.status_code == 400

        res_too_long = client.post(
            "/upload",
            files={"file": ("valid.txt", b"hello", "text/plain")},
            data={"username": "a" * 50},
        )
        assert res_too_long.status_code == 400


def test_api_config_endpoint():
    """10. GET /api/config -> {'max_upload_bytes': 20971520}."""
    with TestClient(main.app) as client:
        res = client.get("/api/config")
        assert res.status_code == 200
        assert res.json() == {"max_upload_bytes": 20 * 1024 * 1024}


def test_history_rendering_and_old_rows():
    """11. Old-style file messages (no suffix, no extra fields) still load through history without errors."""
    # Write directly to the DB an old-style row without original_name or size
    async def insert_legacy():
        import aiosqlite
        async with aiosqlite.connect(database.DB_NAME) as db:
            await db.execute(
                "INSERT INTO messages (username, content, type) VALUES (?, ?, ?)",
                ("olduser", "legacy_manual_file.pdf", "file")
            )
            await db.commit()

    asyncio.run(insert_legacy())

    # Create dummy file so stat works
    legacy_file = main.UPLOAD_DIR / "legacy_manual_file.pdf"
    legacy_file.write_bytes(b"%PDFdummycontent")

    # Connect to WebSocket endpoint and verify history received
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws/alice") as ws:
            data = ws.receive_json()
            assert data["type"] == "history"
            msgs = data["messages"]
            assert len(msgs) >= 1
            leg_msg = next((m for m in msgs if m["content"] == "legacy_manual_file.pdf"), None)
            assert leg_msg is not None
            assert leg_msg["original_name"] == "legacy_manual_file.pdf"
            assert leg_msg["size"] == len(b"%PDFdummycontent")

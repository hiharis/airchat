from contextlib import asynccontextmanager
import aiosqlite
import asyncio

DB_NAME = "airchat.db"

@asynccontextmanager
async def get_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        yield db

async def init_db():
    async with get_db() as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                content TEXT,
                type TEXT NOT NULL, 
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Safe migration for optional file metadata columns
        async with db.execute("PRAGMA table_info(messages)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
        if "original_name" not in columns:
            await db.execute("ALTER TABLE messages ADD COLUMN original_name TEXT")
        if "size" not in columns:
            await db.execute("ALTER TABLE messages ADD COLUMN size INTEGER")

        # Reactions table & index
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reactions (
                message_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                emoji TEXT NOT NULL,
                PRIMARY KEY (message_id, username, emoji)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_reactions_message_id ON reactions (message_id)")
        await db.commit()

async def save_message(username: str, content: str, msg_type: str = "text", original_name: str | None = None, size: int | None = None) -> int:
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO messages (username, content, type, original_name, size) VALUES (?, ?, ?, ?, ?)",
            (username, content, msg_type, original_name, size)
        )
        msg_id = cursor.lastrowid
        await db.commit()
        return msg_id

async def _get_reactions_for_ids(db: aiosqlite.Connection, message_ids: list[int]) -> dict[int, dict[str, list[str]]]:
    if not message_ids:
        return {}
    placeholders = ",".join("?" for _ in message_ids)
    async with db.execute(
        f"SELECT message_id, emoji, username FROM reactions WHERE message_id IN ({placeholders}) ORDER BY rowid ASC",
        tuple(message_ids),
    ) as cursor:
        rows = await cursor.fetchall()
    result: dict[int, dict[str, list[str]]] = {}
    for row in rows:
        mid, emo, u = row[0], row[1], row[2]
        if mid not in result:
            result[mid] = {}
        if emo not in result[mid]:
            result[mid][emo] = []
        result[mid][emo].append(u)
    return result

async def get_history(limit: int = 500) -> list[dict]:
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, username, content, type AS msg_type, timestamp, original_name, size FROM messages WHERE timestamp >= datetime('now', '-5 minutes') ORDER BY id DESC LIMIT ?",
            (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            messages = [dict(row) for row in reversed(rows)]

        if messages:
            msg_ids = [m["id"] for m in messages]
            rx_map = await _get_reactions_for_ids(db, msg_ids)
            for m in messages:
                rx = rx_map.get(m["id"])
                if rx:
                    m["reactions"] = rx
        return messages

async def get_messages_delta(after_id: int, limit: int = 200) -> tuple[str, list[dict], dict[int, dict[str, list[str]]], bool]:
    """
    Returns (mode, messages, reactions_snapshot, truncated).
    - If after_id > current max id (database was reset), returns ("full", [], {}, False).
    - Otherwise returns ("delta", messages, reactions_snapshot, truncated).
    """
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        # Check current max id
        async with db.execute("SELECT MAX(id) FROM messages") as cursor:
            row = await cursor.fetchone()
            max_id = row[0] if (row and row[0] is not None) else 0

        if after_id > max_id:
            return "full", [], {}, False

        # Count messages after after_id
        async with db.execute("SELECT COUNT(*) FROM messages WHERE id > ?", (after_id,)) as cursor:
            cnt_row = await cursor.fetchone()
            total_after = cnt_row[0] if cnt_row else 0

        truncated = False
        if total_after > limit:
            truncated = True
            # Newest limit messages, sorted oldest -> newest
            async with db.execute(
                "SELECT id, username, content, type AS msg_type, timestamp, original_name, size FROM messages WHERE id > ? ORDER BY id DESC LIMIT ?",
                (after_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
                messages = [dict(r) for r in reversed(rows)]
        else:
            async with db.execute(
                "SELECT id, username, content, type AS msg_type, timestamp, original_name, size FROM messages WHERE id > ? ORDER BY id ASC",
                (after_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                messages = [dict(r) for r in rows]

        # Attach reactions for delta messages
        if messages:
            delta_ids = [m["id"] for m in messages]
            rx_map = await _get_reactions_for_ids(db, delta_ids)
            for m in messages:
                rx = rx_map.get(m["id"])
                if rx:
                    m["reactions"] = rx

        # Also get reactions snapshot for the 50 most recent messages in the DB
        async with db.execute("SELECT id FROM messages ORDER BY id DESC LIMIT 50") as cursor:
            recent_rows = await cursor.fetchall()
            recent_ids = [r[0] for r in recent_rows]

        rx_snapshot = await _get_reactions_for_ids(db, recent_ids) if recent_ids else {}

        return "delta", messages, rx_snapshot, truncated

async def toggle_reaction(message_id: int, username: str, emoji: str, allowed_emojis: set[str]) -> tuple[bool, bool]:
    """
    Toggles reaction (same user + emoji removes it).
    Returns (exists, active).
    """
    if emoji not in allowed_emojis:
        return False, False

    async with get_db() as db:
        # Check message exists
        async with db.execute("SELECT id FROM messages WHERE id = ?", (message_id,)) as cursor:
            msg_row = await cursor.fetchone()
            if not msg_row:
                return False, False

        # Check existing reaction
        async with db.execute(
            "SELECT 1 FROM reactions WHERE message_id = ? AND username = ? AND emoji = ?",
            (message_id, username, emoji),
        ) as cursor:
            existing = await cursor.fetchone()

        if existing:
            await db.execute(
                "DELETE FROM reactions WHERE message_id = ? AND username = ? AND emoji = ?",
                (message_id, username, emoji),
            )
            await db.commit()
            return True, False
        else:
            # Check distinct emojis cap (12 max per message)
            async with db.execute(
                "SELECT COUNT(DISTINCT emoji) FROM reactions WHERE message_id = ?",
                (message_id,),
            ) as cursor:
                cnt_row = await cursor.fetchone()
                distinct_count = cnt_row[0] if cnt_row else 0

            if distinct_count >= 12:
                # Cap reached for new distinct emojis
                return True, False

            await db.execute(
                "INSERT INTO reactions (message_id, username, emoji) VALUES (?, ?, ?)",
                (message_id, username, emoji),
            )
            await db.commit()
            return True, True

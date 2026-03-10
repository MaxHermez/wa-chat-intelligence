"""
WhatsApp export parser.
Parses the .txt export into structured messages and stores in SQLite.
"""

import re
import sqlite3
import os
from contextlib import closing
from datetime import datetime
from dateutil import parser as dateparser
from pathlib import Path

from wain import config
from wain.config import DB_PATH, CHAT_TXT_FILE, SENDER_SELF, SENDER_OTHER

# WhatsApp export line pattern: "DD/MM/YYYY, H:MM am/pm - Sender: message"
LINE_RE = re.compile(
    r'^(\d{1,2}/\d{1,2}/\d{4}),\s+(\d{1,2}:\d{2}(?:\s*[ap]m)?)\s+-\s+([^:]+?):\s+(.*)$',
    re.IGNORECASE
)
SYSTEM_RE = re.compile(
    r'^(\d{1,2}/\d{1,2}/\d{4}),\s+(\d{1,2}:\d{2}(?:\s*[ap]m)?)\s+-\s+(.*)$',
    re.IGNORECASE
)
MEDIA_RE = re.compile(r'^([\w\-]+\.\w+)\s+\(file attached\)$')


def normalize_sender(name: str) -> str:
    """Map a raw export sender name to SENDER_SELF or SENDER_OTHER."""
    if name.strip().lower() in config.SENDER_SELF_RAW:
        return SENDER_SELF
    return SENDER_OTHER


def parse_export(filepath: str) -> list[dict]:
    messages = []
    current = None

    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            m = LINE_RE.match(line)
            if m:
                if current:
                    messages.append(current)
                date_str, time_str, sender, body = m.groups()
                try:
                    ts = dateparser.parse(f"{date_str} {time_str}", dayfirst=config.DATE_DAYFIRST)
                except Exception:
                    ts = None

                media_match = MEDIA_RE.match(body.strip())
                media_file = media_match.group(1) if media_match else None
                text = None if media_file else body

                current = {
                    "timestamp": ts.isoformat() if ts else None,
                    "date": ts.date().isoformat() if ts else None,
                    "sender": normalize_sender(sender),
                    "raw_sender": sender.strip(),
                    "text": text,
                    "media_file": media_file,
                    "media_type": _media_type(media_file) if media_file else None,
                    "notes": None,
                }
            elif line.startswith(" ") or (current and not LINE_RE.match(line) and not SYSTEM_RE.match(line)):
                # Continuation of previous message
                if current and current["text"] is not None:
                    current["text"] += "\n" + line.strip()

    if current:
        messages.append(current)

    return messages


def _media_type(filename: str) -> str:
    if not filename:
        return None
    ext = Path(filename).suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return "image"
    if ext in (".mp4", ".mov", ".avi"):
        return "video"
    if ext in (".opus", ".ogg", ".mp3", ".m4a"):
        return "audio"
    if ext in (".pdf", ".doc", ".docx", ".zip"):
        return "document"
    return "other"


def init_db(db_path: str):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            date TEXT,
            sender TEXT,
            raw_sender TEXT,
            text TEXT,
            media_file TEXT,
            media_type TEXT,
            media_path TEXT,
            chunk_id INTEGER,
            notes TEXT,
            transcript TEXT,
            description TEXT
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_start TEXT,
            date_end TEXT,
            msg_start_id INTEGER,
            msg_end_id INTEGER,
            message_count INTEGER,
            summary TEXT,
            embedding_id INTEGER,
            notes TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            text,
            notes,
            content='messages',
            content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, text, notes) VALUES (new.id, new.text, new.notes);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text, notes) VALUES('delete', old.id, old.text, old.notes);
            INSERT INTO messages_fts(rowid, text, notes) VALUES (new.id, new.text, new.notes);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, text, notes)
            VALUES('delete', old.id, old.text, old.notes);
        END;

        CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date);
        CREATE INDEX IF NOT EXISTS idx_messages_chunk_id ON messages(chunk_id);
        CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
    """)
    conn.commit()
    return conn


def resolve_media_path(filename: str) -> str | None:
    if not filename:
        return None
    path = os.path.join(config.CHAT_EXPORT_DIR, filename)
    return path if os.path.exists(path) else None


def insert_messages(conn: sqlite3.Connection, messages: list[dict]):
    """Insert a list of messages unconditionally. Used for first-run and --force re-parse."""
    c = conn.cursor()
    for msg in messages:
        media_path = resolve_media_path(msg["media_file"])
        c.execute("""
            INSERT INTO messages (timestamp, date, sender, raw_sender, text, media_file, media_type, media_path, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            msg["timestamp"],
            msg["date"],
            msg["sender"],
            msg["raw_sender"],
            msg["text"],
            msg["media_file"],
            msg["media_type"],
            media_path,
            msg["notes"],
        ))
    conn.commit()


def insert_messages_delta(conn: sqlite3.Connection, messages: list[dict]) -> tuple[int, int]:
    """
    Delta-aware insert: only add messages not already in the DB.

    Strategy:
    - Find the latest timestamp already stored.
    - Messages with timestamp < latest are skipped (definitely already present).
    - Messages with timestamp >= latest are deduplicated against the DB by
      (timestamp, sender, text) to handle same-minute boundary edge cases.
    - Returns (inserted, skipped).
    """
    c = conn.cursor()

    # First run: nothing in DB yet
    row = c.execute("SELECT MAX(timestamp) FROM messages").fetchone()
    latest_ts = row[0] if row and row[0] else None

    if latest_ts is None:
        insert_messages(conn, messages)
        return len(messages), 0

    # Messages clearly older than the boundary are already in DB -- skip without DB lookup
    candidates = [m for m in messages if m["timestamp"] and m["timestamp"] >= latest_ts]
    n_older_skipped = len(messages) - len(candidates)

    if not candidates:
        return 0, n_older_skipped

    # Load boundary-region messages from DB for exact dedup
    existing = set()
    for row in c.execute(
        "SELECT timestamp, raw_sender, text FROM messages WHERE timestamp >= ?", (latest_ts,)
    ).fetchall():
        existing.add((row[0], row[1], row[2]))

    new_msgs = [
        m for m in candidates
        if (m["timestamp"], m["raw_sender"], m["text"]) not in existing
    ]
    already_present = len(candidates) - len(new_msgs)

    if new_msgs:
        insert_messages(conn, new_msgs)

    return len(new_msgs), n_older_skipped + already_present


if __name__ == "__main__":
    print(f"Chat file:  {CHAT_TXT_FILE}")
    print(f"Media dir:  {config.CHAT_EXPORT_DIR}")
    print(f"DB:         {config.active_db_path()}")
    print()

    print("Parsing WhatsApp export...")
    messages = parse_export(CHAT_TXT_FILE)
    print(f"Parsed {len(messages)} messages")

    print("Initializing DB...")
    conn = init_db(config.active_db_path())

    print("Inserting messages...")
    insert_messages(conn, messages)

    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM messages")
    print(f"Total messages in DB: {c.fetchone()[0]}")

    c.execute("SELECT MIN(date), MAX(date) FROM messages")
    row = c.fetchone()
    print(f"Date range: {row[0]} -> {row[1]}")

    c.execute("SELECT sender, COUNT(*) FROM messages GROUP BY sender")
    for row in c.fetchall():
        print(f"  {row[0]}: {row[1]} messages")

    conn.close()  # __main__ only -- init_db() returns connection, caller manages lifecycle
    print("Done.")

"""
Chunker: groups messages into daily conversation chunks,
then calls the summarizer with sliding context window.
"""

import sqlite3
from contextlib import closing
from collections import defaultdict

from wain import config
from wain.config import DB_PATH

CHUNK_SIZE_DAYS = 1  # one chunk per day


def get_messages_by_day(conn: sqlite3.Connection) -> dict:
    c = conn.cursor()
    c.execute("""
        SELECT id, timestamp, date, sender, text, media_file, media_type
        FROM messages
        ORDER BY timestamp ASC
    """)
    by_day = defaultdict(list)
    for row in c.fetchall():
        msg_id, ts, date, sender, text, media_file, media_type = row
        by_day[date].append({
            "id": msg_id,
            "timestamp": ts,
            "date": date,
            "sender": sender,
            "text": text,
            "media_file": media_file,
            "media_type": media_type,
        })
    return dict(sorted(by_day.items()))


def create_chunks(conn: sqlite3.Connection) -> list[dict]:
    """Full rebuild: wipe all chunks and recreate from scratch. Use update_chunks() for incremental runs."""
    c = conn.cursor()

    # Clear existing chunks
    c.execute("DELETE FROM chunks")
    c.execute("UPDATE messages SET chunk_id = NULL")
    conn.commit()

    by_day = get_messages_by_day(conn)
    chunks = []

    for date, msgs in by_day.items():
        if not msgs:
            continue
        msg_ids = [m["id"] for m in msgs]
        chunk_data = {
            "date_start": date,
            "date_end": date,
            "msg_start_id": min(msg_ids),
            "msg_end_id": max(msg_ids),
            "message_count": len(msgs),
        }
        c.execute("""
            INSERT INTO chunks (date_start, date_end, msg_start_id, msg_end_id, message_count)
            VALUES (:date_start, :date_end, :msg_start_id, :msg_end_id, :message_count)
        """, chunk_data)
        chunk_id = c.lastrowid
        chunk_data["id"] = chunk_id
        chunk_data["messages"] = msgs

        # Tag messages with chunk_id
        c.execute("""
            UPDATE messages SET chunk_id = ?
            WHERE id BETWEEN ? AND ?
            AND date = ?
        """, (chunk_id, chunk_data['msg_start_id'], chunk_data['msg_end_id'], date))
        chunks.append(chunk_data)

    conn.commit()
    print(f"Created {len(chunks)} chunks")
    return chunks




def update_chunks(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """
    Delta-aware chunker. Operates only on dates with new or changed messages.

    For each date in the messages table:
    - SKIP: chunk exists and its message_count matches messages table -> fully processed, no change.
    - EXTEND: chunk exists but message_count in DB is now higher -> boundary chunk grew.
      Clears summary and embedding_id so the chunk gets re-summarized and re-embedded.
    - CREATE: no chunk exists for this date -> new day, create fresh chunk.

    Returns (created, extended, skipped).
    """
    c = conn.cursor()

    # Load existing chunks keyed by date
    existing: dict[str, dict] = {}
    for row in c.execute(
        "SELECT id, date_start, message_count FROM chunks"
    ).fetchall():
        existing[row[1]] = {"id": row[0], "message_count": row[2]}

    # Count messages per day from the messages table (source of truth)
    by_day = get_messages_by_day(conn)

    created = extended = skipped = 0

    for date, msgs in by_day.items():
        if not msgs:
            continue

        msg_ids = [m["id"] for m in msgs]
        total = len(msgs)

        if date in existing:
            ex = existing[date]
            if ex["message_count"] == total:
                # Nothing new for this date
                skipped += 1
                continue

            # Boundary chunk: more messages than when last chunked
            c.execute("""
                UPDATE chunks
                SET msg_end_id = ?,
                    message_count = ?,
                    summary = NULL,
                    embedding_id = NULL
                WHERE id = ?
            """, (max(msg_ids), total, ex["id"]))
            # Tag any unchunked messages for this date
            c.execute(
                "UPDATE messages SET chunk_id = ? WHERE date = ? AND chunk_id IS NULL",
                (ex["id"], date)
            )
            extended += 1

        else:
            # New date -- create chunk
            c.execute("""
                INSERT INTO chunks (date_start, date_end, msg_start_id, msg_end_id, message_count)
                VALUES (?, ?, ?, ?, ?)
            """, (date, date, min(msg_ids), max(msg_ids), total))
            chunk_id = c.lastrowid
            c.execute(
                "UPDATE messages SET chunk_id = ? WHERE date = ? AND chunk_id IS NULL",
                (chunk_id, date)
            )
            created += 1

    conn.commit()
    return created, extended, skipped

def format_chunk_for_summary(msgs: list[dict]) -> str:
    """
    Format messages into a readable block for the summarizer.

    Audio messages with a non-empty transcript are rendered as:
      [HH:MM] Sender: [Voice note: <transcript text>]
    Audio messages without a transcript fall back to:
      [HH:MM] Sender: [audio: <filename>]
    """
    lines = []
    for m in msgs:
        ts = m["timestamp"][:16] if m["timestamp"] else "?"
        # Use raw_sender for summarizer input so group chat participants
        # appear by their actual names rather than normalized "Them"
        sender = m.get("raw_sender") or m["sender"]
        transcript  = m.get("transcript")  or ""
        description = m.get("description") or ""

        if m["text"]:
            lines.append(f"[{ts}] {sender}: {m['text']}")
        elif m["media_type"] == "audio" and transcript.strip():
            lines.append(f"[{ts}] {sender}: [Voice note: {transcript.strip()}]")
        elif m["media_type"] == "image" and description.strip():
            lines.append(f"[{ts}] {sender}: [Image: {description.strip()}]")
        elif m["media_file"]:
            lines.append(f"[{ts}] {sender}: [{m['media_type'] or 'media'}: {m['media_file']}]")
    return "\n".join(lines)


def get_chunk_messages(conn: sqlite3.Connection, chunk_id: int) -> list[dict]:
    """Fetch all messages for a chunk, including transcript for voice notes."""
    c = conn.cursor()
    # transcript and description columns may not exist on older DBs -- handle gracefully
    try:
        c.execute("""
            SELECT id, timestamp, sender, raw_sender, text, media_file, media_type, transcript, description
            FROM messages WHERE chunk_id = ?
            ORDER BY timestamp ASC
        """, (chunk_id,))
        return [
            {"id": r[0], "timestamp": r[1], "sender": r[2], "raw_sender": r[3],
             "text": r[4], "media_file": r[5], "media_type": r[6],
             "transcript": r[7], "description": r[8]}
            for r in c.fetchall()
        ]
    except sqlite3.OperationalError:
        # Fallback for older DBs missing transcript/description columns
        c.execute("""
            SELECT id, timestamp, sender, raw_sender, text, media_file, media_type
            FROM messages WHERE chunk_id = ?
            ORDER BY timestamp ASC
        """, (chunk_id,))
        return [
            {"id": r[0], "timestamp": r[1], "sender": r[2], "raw_sender": r[3],
             "text": r[4], "media_file": r[5], "media_type": r[6],
             "transcript": None, "description": None}
            for r in c.fetchall()
        ]


if __name__ == "__main__":
    with closing(sqlite3.connect(config.active_db_path())) as conn:
        chunks = create_chunks(conn)
    print("Sample chunk dates:", [c["date_start"] for c in chunks[:5]])

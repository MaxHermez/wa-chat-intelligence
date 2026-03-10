"""
describer.py -- image description pipeline.

Reads image messages from the DB, calls the OpenAI vision API, and stores
a brief description in the `messages.description` column.

Fits in the pipeline between parse and chunk (same slot as transcriber):
  parse -> transcribe -> describe -> chunk -> summarize -> embed -> query

Usage:
  python -m wain.describer          # describe all pending images
  wain describe                     # same via CLI
  wain describe --limit 50          # process at most N files
  wain describe --force             # clear existing descriptions and re-run
"""

from __future__ import annotations

import base64
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from wain import config
from wain.config import OPENAI_API_KEY


# -- DB migration --------------------------------------------------------------

def ensure_description_column(conn: sqlite3.Connection) -> None:
    """Add description column to messages table if it doesn't exist yet."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "description" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN description TEXT")
        conn.commit()


# -- Vision API ----------------------------------------------------------------

_VISION_PROMPT = (
    "Describe this image briefly in 1-2 sentences. "
    "Focus on what's shown, any visible text, and emotional context if relevant."
)


def _describe_image(media_path: str, model: str) -> str | None:
    """
    Call the OpenAI vision API for a single image file.

    Returns the description string, or None on failure.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Required for VISION_BACKEND=api."
        )

    try:
        with open(media_path, "rb") as f:
            raw = f.read()
    except OSError:
        return None

    b64 = base64.b64encode(raw).decode()
    # Guess MIME type from extension
    ext = Path(media_path).suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    response = client.chat.completions.create(
        model=model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": _VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        max_completion_tokens=150,
    )
    text = response.choices[0].message.content
    return text.strip() if text else None


# -- Main pipeline -------------------------------------------------------------

def run_description(
    db_path: str | None = None,
    limit: int | None = None,
    force: bool = False,
) -> dict:
    """
    Describe all image messages that don't have a description yet.

    Returns a summary dict: described, skipped_no_file, already_done, errors.
    Idempotent: messages with an existing description are skipped.
    Set VISION_BACKEND=none to disable without error.
    """
    db_path = db_path or config.active_db_path()
    backend = config.VISION_BACKEND
    model   = config.VISION_MODEL

    if backend == "none":
        print("VISION_BACKEND=none -- skipping image description.")
        return {"described": 0, "skipped_no_file": 0, "already_done": 0, "errors": 0, "skipped_backend": True}

    conn = sqlite3.connect(db_path)
    try:
        ensure_description_column(conn)

        if force:
            conn.execute("UPDATE messages SET description = NULL WHERE media_type = 'image'")
            conn.commit()

        rows = conn.execute("""
            SELECT id, media_path, media_file
            FROM messages
            WHERE media_type = 'image'
              AND description IS NULL
              AND media_path IS NOT NULL
            ORDER BY id ASC
        """).fetchall()

        if limit:
            rows = rows[:limit]

        total_pending  = len(rows)
        described      = 0
        skipped_no_file = 0
        errors         = 0

        for i, (msg_id, media_path, media_file) in enumerate(rows, 1):
            if not media_path or not os.path.exists(media_path):
                conn.execute("UPDATE messages SET description = '' WHERE id = ?", (msg_id,))
                conn.commit()
                skipped_no_file += 1
                print(f"  [{i}/{total_pending}] id={msg_id} {media_file}: no file -- skipped")
                continue

            print(f"  [{i}/{total_pending}] id={msg_id} {media_file} ...", end=" ", flush=True)
            try:
                desc = _describe_image(media_path, model)
                conn.execute("UPDATE messages SET description = ? WHERE id = ?", (desc or "", msg_id))
                conn.commit()
                if desc:
                    described += 1
                    print("ok")
                else:
                    skipped_no_file += 1
                    print("empty response")
            except (OSError, FileNotFoundError) as e:
                errors += 1
                print(f"[skip] {e}")
                # Permanent failure (bad file) -- store '' so we never retry
                conn.execute("UPDATE messages SET description = '' WHERE id = ?", (msg_id,))
                conn.commit()
            except Exception as e:
                errors += 1
                print(f"[error] {e} (will retry)")
                # Transient failure (API error, rate limit, timeout) -- leave NULL so next run retries
    finally:
        conn.close()

    with closing(sqlite3.connect(db_path)) as conn2:
        already_done = conn2.execute(
            "SELECT COUNT(*) FROM messages WHERE media_type='image' AND description IS NOT NULL"
        ).fetchone()[0]

    return {
        "described":       described,
        "skipped_no_file": skipped_no_file,
        "already_done":    already_done,
        "errors":          errors,
    }


if __name__ == "__main__":
    import sys
    limit = None
    force = False
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1]); i += 2
        elif args[i] == "--force":
            force = True; i += 1
        else:
            i += 1

    result = run_description(limit=limit, force=force)
    print(f"\nDone: {result['described']} described, "
          f"{result['skipped_no_file']} skipped (no file), "
          f"{result['errors']} errors, "
          f"{result['already_done']} total with description in DB.")

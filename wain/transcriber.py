"""
transcriber.py -- voice note transcription pipeline.

Reads audio messages from the DB, transcribes them using Whisper
(local CLI or OpenAI API), and stores the transcript in the
`messages.transcript` column.

Fits in the pipeline between parse and chunk:
  parse -> transcribe -> chunk -> summarize -> embed -> query

Usage:
  python -m wain.transcriber          # transcribe all pending audio
  wain transcribe                     # same via CLI
  wain transcribe --limit 50          # process at most N files
  wain transcribe --backend api       # override backend for this run
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from contextlib import closing
import tempfile
import time
from pathlib import Path

from wain import config
from wain.config import (
    DB_PATH, OPENAI_API_KEY,
    WHISPER_BACKEND, WHISPER_MODEL, WHISPER_LANGUAGE,
)


# -- DB migration --------------------------------------------------------------

def ensure_transcript_column(conn: sqlite3.Connection) -> None:
    """Add transcript column to messages table if it doesn't exist yet."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "transcript" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN transcript TEXT")
        conn.commit()


# -- Backends ------------------------------------------------------------------

def _transcribe_local(media_path: str, model: str, language: str) -> str | None:
    """
    Transcribe using the `whisper` CLI (openai-whisper package).

    Runs whisper in a temp dir, reads the .txt output file.
    Returns transcript text, or None on failure.
    """
    cmd = ["whisper", media_path, "--model", model, "--output_format", "txt"]
    if language:
        cmd += ["--language", language]

    with tempfile.TemporaryDirectory() as tmpdir:
        cmd += ["--output_dir", tmpdir]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            return None
        except FileNotFoundError:
            raise RuntimeError(
                "whisper CLI not found. Install openai-whisper: pip install openai-whisper"
            )

        if result.returncode != 0:
            return None

        # Whisper writes <stem>.txt in the output dir
        stem = Path(media_path).stem
        txt_path = Path(tmpdir) / f"{stem}.txt"
        if txt_path.exists():
            text = txt_path.read_text(encoding="utf-8").strip()
            return text if text else None

    return None


def _transcribe_api(media_path: str, model: str, language: str) -> str | None:
    """
    Transcribe using the OpenAI Audio Transcriptions API.

    Requires OPENAI_API_KEY to be set.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Required for WHISPER_BACKEND=api."
        )
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    kwargs: dict = {"model": model}
    if language:
        kwargs["language"] = language

    # OpenAI Whisper API rejects .opus extension (not in its allowlist) even though
    # .opus is codec-compatible with .ogg containers. Override filename for .opus files.
    fname = "audio.ogg" if media_path.endswith(".opus") else os.path.basename(media_path)
    with open(media_path, "rb") as f:
        response = client.audio.transcriptions.create(file=(fname, f), **kwargs)
    return response.text.strip() or None


def transcribe_file(
    media_path: str,
    backend: str = WHISPER_BACKEND,
    model: str = WHISPER_MODEL,
    language: str = WHISPER_LANGUAGE,
) -> str | None:
    """
    Transcribe a single audio file. Returns transcript string or None on failure.
    Dispatches to the configured backend.
    """
    if backend == "api":
        return _transcribe_api(media_path, model, language)
    else:
        return _transcribe_local(media_path, model, language)


# -- Main transcription pass ---------------------------------------------------

def run_transcription(
    db_path: str | None = None,
    backend: str = WHISPER_BACKEND,
    model: str = WHISPER_MODEL,
    language: str = WHISPER_LANGUAGE,
    limit: int | None = None,
) -> dict:
    """
    Transcribe all audio messages that don't have a transcript yet.

    Returns a summary dict with counts: transcribed, skipped_no_file,
    already_done, errors.

    Idempotent: messages with an existing transcript are skipped.
    """
    db_path = db_path or config.active_db_path()
    conn = sqlite3.connect(db_path)
    try:
        ensure_transcript_column(conn)

        # Fetch audio messages that need transcription
        rows = conn.execute("""
            SELECT id, media_path, media_file
            FROM messages
            WHERE media_type = 'audio'
              AND transcript IS NULL
            ORDER BY id ASC
        """).fetchall()

        if limit:
            rows = rows[:limit]

        total_pending = len(rows)
        transcribed = 0
        skipped_no_file = 0
        errors = 0
        t0 = time.monotonic()

        for msg_id, media_path, media_file in rows:
            # File existence check -- skip gracefully, no crash
            if not media_path or not Path(media_path).exists():
                skipped_no_file += 1
                continue

            try:
                transcript = transcribe_file(media_path, backend=backend, model=model, language=language)
            except Exception as e:
                print(f"  [msg {msg_id}] ERROR: {e}")
                errors += 1
                continue

            if transcript is None:
                # Whisper ran but produced no output (silence, noise, etc.)
                # Store empty string so we don't retry indefinitely
                transcript = ""

            conn.execute(
                "UPDATE messages SET transcript = ? WHERE id = ?",
                (transcript, msg_id)
            )
            conn.commit()
            transcribed += 1

            elapsed = time.monotonic() - t0
            rate = transcribed / elapsed if elapsed > 0 else 0
            remaining = total_pending - transcribed - skipped_no_file - errors
            eta = f" ETA ~{remaining/rate:.0f}s" if rate > 0 and remaining > 0 else ""
            print(
                f"  [{transcribed:4d}/{total_pending}] msg {msg_id}  "
                f"{(transcript or '[silence]')[:60].replace(chr(10), ' ')}"
                f"{eta}",
                flush=True,
            )
    finally:
        conn.close()

    with closing(sqlite3.connect(db_path)) as conn2:
        already_done = conn2.execute(
            "SELECT COUNT(*) FROM messages WHERE media_type='audio' AND transcript IS NOT NULL"
        ).fetchone()[0]

    return {
        "transcribed": transcribed,
        "skipped_no_file": skipped_no_file,
        "errors": errors,
        "already_done": already_done,
        "total_audio": already_done + (total_pending - transcribed - skipped_no_file - errors),
    }


if __name__ == "__main__":
    import sys
    backend = WHISPER_BACKEND
    model = WHISPER_MODEL
    limit = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--backend" and i + 1 < len(args):
            backend = args[i + 1]; i += 2
        elif args[i] == "--model" and i + 1 < len(args):
            model = args[i + 1]; i += 2
        elif args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1]); i += 2
        else:
            i += 1

    print(f"Backend: {backend}  Model: {model}  Limit: {limit or 'all'}")
    result = run_transcription(backend=backend, model=model, limit=limit)
    print(f"\nDone: {result['transcribed']} transcribed, "
          f"{result['skipped_no_file']} skipped (no file), "
          f"{result['errors']} errors, "
          f"{result['already_done']} already done")

"""
Summarizer (HHC-8): Strictly sequential LLM summarization pipeline.

Design:
- Processes all unsummarized chunks in strict id order (never parallel)
- For each chunk: includes previous 1-2 summaries + up to 3 FAISS-retrieved
  relevant earlier summaries above similarity threshold (if FAISS index exists)
- Uses AsyncOpenAI client for API efficiency -- still sequential via awaited for loop
- Saves to DB after each chunk (resume-safe)
- Model: gpt-5-mini with max_completion_tokens

Usage:
    python summarizer.py              # process all unsummarized chunks
    python summarizer.py --from 33    # resume from chunk id 33
    python summarizer.py --dry-run    # show plan without calling API
"""

import asyncio
import json
import os
import sys
import time

from openai import AsyncOpenAI
from wain.chunker import get_chunk_messages, format_chunk_for_summary
from wain import config
from wain.config import OPENAI_API_KEY, SENDER_SELF, SENDER_OTHER, SUMMARIZER_CONTEXT
from wain.schema import (
    JSON_SCHEMA_BLOCK, REQUIRED_FIELDS, parse_summary_json,
    validate_summary, validation_issues, serialize_summary
)
REQUIRED_FIELDS_STR = sorted(REQUIRED_FIELDS)

# -- Config ----------------------------------------------------------------------

MODEL = config.SUMMARIZER_MODEL
MAX_COMPLETION_TOKENS = 8000
RECENT_CONTEXT_N = 2        # sliding window: previous N summaries
FAISS_TOP_K = 3             # max relevant summaries from FAISS
FAISS_THRESHOLD = config.FAISS_THRESHOLD  # centralised in config.py
RETRY_ATTEMPTS = 3
RETRY_DELAY_SEC = 5

# -- System prompt ---------------------------------------------------------------

def _build_system_prompt() -> str:
    """Build the summarizer system prompt from config values."""
    schema_block = JSON_SCHEMA_BLOCK.replace("SENDER_SELF", SENDER_SELF).replace("SENDER_OTHER", SENDER_OTHER)
    return f"""{SUMMARIZER_CONTEXT}

Your job is to summarize a single day's conversation chunk. Be thorough and specific:
- What topics were discussed?
- What was the emotional tone and energy level?
- Any significant moments, confessions, plans made or cancelled?
- Any tension, warmth, distance, or shifts in dynamic?
- Media exchanged (voice notes content if described, photos context)?
- Who was more active/engaged?
- Anything that needs context from previous conversations to understand?

{schema_block}"""


SYSTEM_PROMPT = _build_system_prompt()


# -- Context retrieval -----------------------------------------------------------

import sqlite3

def get_recent_summaries(conn, before_chunk_id, n=2):
    """Sliding window: get n most recent summaries before this chunk."""
    c = conn.cursor()
    c.execute("""
        SELECT date_start, summary FROM chunks
        WHERE id < ? AND summary IS NOT NULL
        ORDER BY id DESC LIMIT ?
    """, (before_chunk_id, n))
    rows = c.fetchall()
    return [f"[{r[0]}] {r[1]}" for r in reversed(rows)]


def get_relevant_summaries_faiss(conn, before_chunk_id, query_text, top_k=FAISS_TOP_K, threshold=FAISS_THRESHOLD):
    """Semantic retrieval: FAISS hits from earlier chunks above threshold.
    Returns [] if index does not exist yet (embedder not yet run).
    """
    try:
        import faiss
        import numpy as np
        from wain.embedder import get_embedding
        _idx = config.active_index_path(); _meta = config.active_meta_path()

        if not os.path.exists(_idx):
            return []

        index = faiss.read_index(_idx)
        if index.ntotal == 0:
            return []

        with open(_meta) as f:
            meta = json.load(f)
        emb_to_chunk = {int(k): int(v) for k, v in meta.get("embedding_id_to_chunk_id", {}).items()}

        c = conn.cursor()
        c.execute(
            "SELECT id, date_start, summary, embedding_id FROM chunks "
            "WHERE id < ? AND summary IS NOT NULL AND embedding_id IS NOT NULL",
            (before_chunk_id,)
        )
        candidates = {r[3]: (r[0], r[1], r[2]) for r in c.fetchall()}
        if not candidates:
            return []

        query_vec = np.array([get_embedding(query_text[:500])], dtype="float32")
        faiss.normalize_L2(query_vec)

        D, I = index.search(query_vec, min(top_k * 3, index.ntotal))

        results = []
        for dist, emb_id in zip(D[0], I[0]):
            if emb_id in candidates and dist >= threshold:
                _, date, summary = candidates[emb_id]
                results.append(f"[{date}] {summary}")
                if len(results) >= top_k:
                    break

        return results

    except Exception:
        return []


# -- Core summarization ----------------------------------------------------------

def build_user_message(chunk, conversation_text, recent, relevant):
    """Assemble the user prompt for a single chunk."""
    context_parts = []
    if recent:
        context_parts.append("RECENT CONTEXT (previous 1-2 days):\n" + "\n\n".join(recent))
    if relevant:
        context_parts.append("RELEVANT EARLIER CONTEXT (semantically similar days):\n" + "\n\n".join(relevant))

    context_block = "\n\n---\n\n".join(context_parts)

    parts = [
        f"Date: {chunk['date_start']}",
        f"Messages: {chunk['message_count']}",
        "",
    ]
    if context_block:
        parts += [f"PRIOR CONTEXT:\n{context_block}", "", "---", ""]
    parts += ["CONVERSATION:", conversation_text]

    return "\n".join(parts)


async def summarize_one(client, conn, chunk):
    """Summarize a single chunk. Validates schema, retries on failure. Returns clean JSON str or None."""
    msgs = get_chunk_messages(conn, chunk["id"])
    if not msgs:
        return None

    conversation_text = format_chunk_for_summary(msgs)
    recent = get_recent_summaries(conn, chunk["id"], n=RECENT_CONTEXT_N)
    relevant = get_relevant_summaries_faiss(conn, chunk["id"], conversation_text[:500])

    user_msg = build_user_message(chunk, conversation_text, recent, relevant)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            response = await client.chat.completions.create(
                model=MODEL,
                max_completion_tokens=MAX_COMPLETION_TOKENS,
                response_format={"type": "json_object"},
                messages=messages,
            )
            content = response.choices[0].message.content
            if not content or not content.strip():
                return None

            # Validate schema; retry once with a correction prompt if needed
            data = parse_summary_json(content)
            if data is None:
                issues_list = ["Response is not valid JSON"]
            else:
                # Normalize aliases before validating (e.g. emotional_tone -> mood)
                from wain.schema import normalize_summary
                data = normalize_summary(data)
                issues_list = validation_issues(data)

            if issues_list and attempt < RETRY_ATTEMPTS:
                correction = (
                    f"Your response had schema issues: {'; '.join(issues_list)}. "
                    f"Return a corrected JSON object that includes ALL required fields: "
                    f"{sorted(REQUIRED_FIELDS_STR)}. "
                    f"Use the canonical field names (e.g. 'mood' not 'emotional_tone')."
                )
                messages = messages + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": correction},
                ]
                print(f"\n    [schema retry: {issues_list}]", end="  ", flush=True)
                continue

            if data is not None:
                # Return clean, normalized, control-char-free JSON
                return serialize_summary(data)
            return content.strip()

        except Exception as e:
            if attempt < RETRY_ATTEMPTS:
                print(f"\n    [attempt {attempt} failed: {e}] retrying in {RETRY_DELAY_SEC}s...", flush=True)
                await asyncio.sleep(RETRY_DELAY_SEC)
            else:
                print(f"\n    [ERROR after {RETRY_ATTEMPTS} attempts: {e}]", flush=True)
                return None


# -- Main run loop ---------------------------------------------------------------

async def run(start_from_chunk_id=1, dry_run=False):
    """
    Strictly sequential summarization.
    Processes all unsummarized chunks in strict id order (never parallel).
    start_from_chunk_id: skip chunks with id < this value.
    """
    if not OPENAI_API_KEY:
        raise ValueError(
            "OPENAI_API_KEY is not set. "
            "Export it as an environment variable or add it to your .env file."
        )

    conn = sqlite3.connect(config.active_db_path())
    try:
        c = conn.cursor()

        c.execute("""
            SELECT id, date_start, date_end, message_count
            FROM chunks
            WHERE summary IS NULL AND id >= ?  -- delta: skips already-summarized chunks
            ORDER BY id ASC
        """, (start_from_chunk_id,))
        pending = [
            {"id": r[0], "date_start": r[1], "date_end": r[2], "message_count": r[3]}
            for r in c.fetchall()
        ]

        total = len(pending)
        print(f"{'[DRY RUN] ' if dry_run else ''}Sequential summarizer starting.")
        print(f"  DB:       {config.active_db_path()}")
        print(f"  Chunks to process: {total}")
        print(f"  Model: {MODEL}  max_completion_tokens={MAX_COMPLETION_TOKENS}")
        print(f"  Context: {RECENT_CONTEXT_N} recent + up to {FAISS_TOP_K} FAISS-retrieved (if index exists)")
        print()

        if dry_run:
            for i, ch in enumerate(pending):
                recent = get_recent_summaries(conn, ch["id"], n=RECENT_CONTEXT_N)
                print(f"  [{i+1:3d}/{total}] id={ch['id']:3d}  {ch['date_start']}  {ch['message_count']:4d} msgs  "
                      f"context={len(recent)} recent summaries")
            return

        client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        start_time = time.monotonic()
        done = 0
        errors = 0

        for i, chunk in enumerate(pending):
            elapsed = time.monotonic() - start_time
            if done > 0:
                eta_per = elapsed / done
                eta_remaining = eta_per * (total - i)
                eta_str = f"  ETA ~{eta_remaining/60:.1f}min"
            else:
                eta_str = ""

            print(
                f"[{i+1:3d}/{total}] id={chunk['id']:3d}  {chunk['date_start']}  "
                f"{chunk['message_count']:4d} msgs{eta_str}",
                end="  ", flush=True
            )

            summary = await summarize_one(client, conn, chunk)

            if summary:
                c.execute("UPDATE chunks SET summary = ? WHERE id = ?", (summary, chunk["id"]))
                conn.commit()
                print("ok", flush=True)
                done += 1
            else:
                print("skipped", flush=True)
                errors += 1

            # Small pause to be respectful of rate limits
            await asyncio.sleep(0.3)

        elapsed_total = time.monotonic() - start_time
        print()
        print(f"Done. {done} summarized, {errors} errors. Total time: {elapsed_total/60:.1f} min.")
    finally:
        conn.close()


def main():
    start_id = 1
    dry_run = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] in ("--from", "-f") and i + 1 < len(args):
            start_id = int(args[i + 1])
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            i += 1

    asyncio.run(run(start_from_chunk_id=start_id, dry_run=dry_run))


if __name__ == "__main__":
    main()

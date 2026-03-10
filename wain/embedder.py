"""
Embedder: generates embeddings for chunk summaries and notes,
stores them in a FAISS index, and keeps the index in sync with the DB.
"""

import os
import sqlite3
import json
from contextlib import closing
import numpy as np
import faiss
from openai import OpenAI

from wain import config
from wain.config import OPENAI_API_KEY

EMBEDDING_MODEL = config.EMBEDDING_MODEL
EMBEDDING_DIM = 1536


def get_openai_client() -> OpenAI:
    if not OPENAI_API_KEY:
        raise ValueError(
            "OPENAI_API_KEY is not set. "
            "Export it as an environment variable or add it to your .env file."
        )
    return OpenAI(api_key=OPENAI_API_KEY)


# Lazy singleton — created once per process, reused across all embedding calls
_openai_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = get_openai_client()
    return _openai_client


def get_embedding(text: str, client=None) -> list[float]:
    if client is None:
        client = _get_client()
    response = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=text[:8000]  # max safe input
    )
    return response.data[0].embedding


def load_or_create_index() -> tuple[faiss.Index, dict]:
    """Load existing FAISS index or create a new one."""
    _idx = config.active_index_path(); _meta = config.active_meta_path()
    if os.path.exists(_idx) and os.path.exists(_meta):
        index = faiss.read_index(_idx)
        with open(_meta) as f:
            meta = json.load(f)
    else:
        index = faiss.IndexFlatIP(EMBEDDING_DIM)  # Inner product (cosine after normalize)
        meta = {"embedding_id_to_chunk_id": {}, "next_id": 0}
    return index, meta


def save_index(index: faiss.Index, meta: dict):
    faiss.write_index(index, config.active_index_path())
    with open(config.active_meta_path(), "w") as f:
        json.dump(meta, f)


def embed_chunk_summaries():
    """Embed all chunk summaries that don't have embeddings yet."""
    conn = sqlite3.connect(config.active_db_path())
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, date_start, summary, notes FROM chunks
            WHERE summary IS NOT NULL AND embedding_id IS NULL  -- delta: skips already-embedded chunks
            ORDER BY id ASC
        """)
        chunks = c.fetchall()

        if not chunks:
            print("No new chunks to embed.")
            return

        print(f"Embedding {len(chunks)} chunk summaries...")
        client = _get_client()
        index, meta = load_or_create_index()

        for chunk_id, date, summary, notes in chunks:
            # Combine summary + notes for richer embedding
            text_to_embed = summary
            if notes:
                text_to_embed += f"\n\nNotes: {notes}"

            try:
                vec = get_embedding(text_to_embed, client)
                vec_np = np.array([vec], dtype="float32")
                faiss.normalize_L2(vec_np)

                emb_id = meta["next_id"]
                index.add(vec_np)
                meta["embedding_id_to_chunk_id"][str(emb_id)] = chunk_id
                meta["next_id"] += 1

                c.execute("UPDATE chunks SET embedding_id = ? WHERE id = ?", (emb_id, chunk_id))
                conn.commit()
                print(f"  [{date}] embedded as #{emb_id}")

            except Exception as e:
                print(f"  [{date}] ERROR: {e}")

        save_index(index, meta)
    finally:
        conn.close()
    print("Embedding complete.")


def search(query: str, top_k: int = 5, threshold: float | None = None) -> list[dict]:
    """Semantic search over chunk summaries."""
    index, meta = load_or_create_index()
    if index.ntotal == 0:
        return []

    threshold = threshold if threshold is not None else config.FAISS_THRESHOLD

    query_vec = np.array([get_embedding(query)], dtype="float32")
    faiss.normalize_L2(query_vec)
    D, I = index.search(query_vec, min(top_k * 2, index.ntotal))

    results = []
    id_map = meta["embedding_id_to_chunk_id"]
    with closing(sqlite3.connect(config.active_db_path())) as conn:
        c = conn.cursor()
        for score, emb_id in zip(D[0], I[0]):
            if score < threshold:
                continue
            chunk_id = id_map.get(str(emb_id))
            if not chunk_id:
                continue
            c.execute("""
                SELECT id, date_start, date_end, message_count, summary, notes
                FROM chunks WHERE id = ?
            """, (chunk_id,))
            row = c.fetchone()
            if row:
                results.append({
                    "score": float(score),
                    "chunk_id": row[0],
                    "date_start": row[1],
                    "date_end": row[2],
                    "message_count": row[3],
                    "summary": row[4],
                    "notes": row[5],
                })
            if len(results) >= top_k:
                break
    return results


def get_chunk_messages_raw(chunk_id: int) -> list[dict]:
    """Retrieve raw messages for a chunk."""
    with closing(sqlite3.connect(config.active_db_path())) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, timestamp, sender, text, media_file, media_type, notes
            FROM messages WHERE chunk_id = ?
            ORDER BY timestamp ASC
        """, (chunk_id,))
        rows = c.fetchall()
    return [
        {"id": r[0], "timestamp": r[1], "sender": r[2],
         "text": r[3], "media_file": r[4], "media_type": r[5], "notes": r[6]}
        for r in rows
    ]


def add_note(conn: sqlite3.Connection, chunk_id: int = None, msg_id: int = None, note: str = ""):
    """Add a note to a chunk or message, then re-embed the chunk."""
    c = conn.cursor()
    if msg_id:
        c.execute("UPDATE messages SET notes = ? WHERE id = ?", (note, msg_id))
    if chunk_id:
        c.execute("UPDATE chunks SET notes = ?, embedding_id = NULL WHERE id = ?", (note, chunk_id))
    conn.commit()

    # Re-embed if chunk note changed
    if chunk_id:
        c.execute("SELECT summary FROM chunks WHERE id = ?", (chunk_id,))
        row = c.fetchone()
        if row and row[0]:
            embed_chunk_summaries()


if __name__ == "__main__":
    embed_chunk_summaries()

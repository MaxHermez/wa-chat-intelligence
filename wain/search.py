"""
search.py -- hybrid semantic + keyword search over chunk summaries.

Algorithm:
  1. FAISS cosine similarity search over summary embeddings (semantic)
  2. FTS5 BM25 keyword search over chunks_fts (keyword)
  3. Normalize BM25 scores to [0, 1]: best match = 1.0
  4. Combine: combined = w_semantic * semantic + w_keyword * keyword
  5. Source attribution: "semantic" | "keyword" | "both"

Results in both sets score higher because both components are non-zero.
Weights are configurable via config.py or WAINTEL_HYBRID_* env vars.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from typing import Literal

from wain import config
from wain.schema import parse_summary_json

Source = Literal["semantic", "keyword", "both"]

# -- chunks_fts table ----------------------------------------------------------

_CHUNKS_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    date_start,
    summary,
    notes,
    content='chunks',
    content_rowid='id'
);
"""

_CHUNKS_FTS_TRIGGER_AI = """
CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, date_start, summary, notes)
    VALUES (new.id, new.date_start, new.summary, new.notes);
END;
"""

_CHUNKS_FTS_TRIGGER_AU = """
CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, date_start, summary, notes)
    VALUES ('delete', old.id, old.date_start, old.summary, old.notes);
    INSERT INTO chunks_fts(rowid, date_start, summary, notes)
    VALUES (new.id, new.date_start, new.summary, new.notes);
END;
"""

_CHUNKS_FTS_TRIGGER_AD = """
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, date_start, summary, notes)
    VALUES('delete', old.id, old.date_start, old.summary, old.notes);
END;
"""


def ensure_chunks_fts(conn: sqlite3.Connection) -> bool:
    """
    Create chunks_fts FTS5 table and sync triggers if they don't exist.
    Populates the index from existing chunks data on first creation.
    Returns True if the index was newly created (and rebuilt), False if already existed.
    """
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
    ).fetchall()}

    already_exists = "chunks_fts" in tables
    conn.executescript(_CHUNKS_FTS_DDL + _CHUNKS_FTS_TRIGGER_AI + _CHUNKS_FTS_TRIGGER_AU + _CHUNKS_FTS_TRIGGER_AD)
    conn.commit()

    if not already_exists:
        # Rebuild index from existing chunks data
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        conn.commit()
        return True

    return False


# -- FTS5 keyword search -------------------------------------------------------

def _fts_fallback_query(raw: str) -> str:
    """Tokenise raw query into quoted words for FTS5."""
    words = re.findall(r'\w+', raw)
    if not words:
        return '""'
    return " OR ".join(f'"{w}"' for w in words)


def search_fts_chunks(
    query: str,
    conn: sqlite3.Connection,
    limit: int = 20,
) -> list[dict]:
    """
    FTS5 BM25 keyword search over chunk summaries.

    Returns list of dicts with keys: chunk_id, date_start, bm25_score (raw, negative).
    Scores are raw BM25 values -- normalise before combining with semantic scores.
    """
    # First attempt: raw query verbatim; second: quoted tokens (OR) if FTS5 rejects it
    for attempt, fts_query in enumerate([query, _fts_fallback_query(query)]):
        try:
            rows = conn.execute("""
                SELECT
                    c.id,
                    c.date_start,
                    c.date_end,
                    c.message_count,
                    c.summary,
                    c.notes,
                    bm25(chunks_fts) AS bm25_score
                FROM chunks c
                JOIN chunks_fts ON chunks_fts.rowid = c.id
                WHERE chunks_fts MATCH ?
                ORDER BY bm25_score
                LIMIT ?
            """, (fts_query, limit)).fetchall()
            return [
                {
                    "chunk_id":     r[0],
                    "date_start":   r[1],
                    "date_end":     r[2],
                    "message_count": r[3],
                    "summary":      r[4],
                    "notes":        r[5],
                    "bm25_score":   r[6],
                }
                for r in rows
            ]
        except sqlite3.OperationalError:
            if attempt == 0:
                continue  # retry with fallback
            return []

    return []


def _normalize_bm25(results: list[dict]) -> list[dict]:
    """
    Normalize raw BM25 scores (negative) to [0, 1].
    Best match (most negative) -> 1.0. Worst -> approaches 0.
    """
    if not results:
        return results
    min_score = min(r["bm25_score"] for r in results)
    if min_score == 0:
        for r in results:
            r["keyword_score"] = 0.0
    else:
        for r in results:
            r["keyword_score"] = r["bm25_score"] / min_score
    return results


# -- Hybrid search -------------------------------------------------------------

def hybrid_search(
    query: str,
    top_k: int = 5,
    semantic_weight: float | None = None,
    keyword_weight: float | None = None,
    semantic_threshold: float | None = None,
    fts_limit: int | None = None,
) -> list[dict]:
    """
    Hybrid semantic + keyword search over chunk summaries.

    Runs both searches in the same process (sequential, fast).
    Returns a single ranked list with combined scores and source attribution.

    Each result dict contains:
        chunk_id, date_start, date_end, message_count, summary, notes,
        combined_score, semantic_score, keyword_score, source, summary_parsed
    """
    w_sem = semantic_weight if semantic_weight is not None else config.HYBRID_SEMANTIC_WEIGHT
    w_kw  = keyword_weight  if keyword_weight  is not None else config.HYBRID_KEYWORD_WEIGHT
    fts_n = fts_limit or max(top_k * 3, 20)

    # Hybrid uses a lower threshold than standalone semantic search:
    # FTS5 keyword score compensates for borderline semantic matches,
    # so casting a wider net (default: half of FAISS_THRESHOLD) improves recall.
    if semantic_threshold is None:
        semantic_threshold = max(config.FAISS_THRESHOLD * 0.5, 0.10)
    with closing(sqlite3.connect(config.active_db_path())) as conn:
        ensure_chunks_fts(conn)

        # -- semantic results --
        from wain.embedder import search as faiss_search
        sem_raw = faiss_search(query, top_k=max(top_k * 2, 10), threshold=semantic_threshold)
        sem_by_chunk: dict[int, float] = {r["chunk_id"]: r["score"] for r in sem_raw}

        # -- keyword results --
        kw_raw = search_fts_chunks(query, conn, limit=fts_n)
        kw_norm = _normalize_bm25(kw_raw)
        kw_by_chunk: dict[int, dict] = {r["chunk_id"]: r for r in kw_norm}

        # -- fetch full chunk data for semantic-only hits --
        all_chunk_ids = set(sem_by_chunk) | set(kw_by_chunk)
        if not all_chunk_ids:
            return []

        placeholders = ",".join("?" * len(all_chunk_ids))
        chunk_rows = {
            r[0]: r for r in conn.execute(f"""
                SELECT id, date_start, date_end, message_count, summary, notes
                FROM chunks WHERE id IN ({placeholders})
            """, list(all_chunk_ids)).fetchall()
        }

    # -- combine scores --
    results: list[dict] = []
    for chunk_id in all_chunk_ids:
        sem_score = sem_by_chunk.get(chunk_id, 0.0)
        kw_score  = kw_by_chunk.get(chunk_id, {}).get("keyword_score", 0.0)
        combined  = w_sem * sem_score + w_kw * kw_score

        if chunk_id not in chunk_rows:
            continue
        row = chunk_rows[chunk_id]

        if sem_score > 0 and kw_score > 0:
            source: Source = "both"
        elif sem_score > 0:
            source = "semantic"
        else:
            source = "keyword"

        results.append({
            "chunk_id":      chunk_id,
            "date_start":    row[1],
            "date_end":      row[2],
            "message_count": row[3],
            "summary":       row[4],
            "notes":         row[5],
            "combined_score": round(combined, 4),
            "semantic_score": round(sem_score, 4),
            "keyword_score":  round(kw_score, 4),
            "source":         source,
            "summary_parsed": parse_summary_json(row[4]),
        })

    results.sort(key=lambda r: r["combined_score"], reverse=True)
    return results[:top_k]

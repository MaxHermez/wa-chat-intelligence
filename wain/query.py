"""
Query interface: semantic search + full-text search + date-based retrieval.
The main interface for answering questions about the conversation.

Usage (interactive CLI):
    python query.py                   # interactive mode
    python query.py "your question"   # one-shot query
    python query.py --stats           # show DB stats
    python query.py --date 2025-10-15 # summaries for a specific date
"""

import json
import re
import sqlite3
import sys
from contextlib import closing

from wain.embedder import search, get_chunk_messages_raw
from wain import config
from wain.config import SENDER_SELF, SENDER_OTHER
from wain.schema import parse_summary_json as _parse_json
from wain.search import hybrid_search  # noqa: F401 -- re-exported


# -- Summary parsing ---------------------------------------------------------

def parse_summary(raw: str):
    """Parse a chunk summary -- delegates to wain.schema for consistent handling."""
    return _parse_json(raw)


def format_summary_markdown(raw: str, date: str = "", score=None) -> str:
    """Render a chunk summary as clean markdown. Falls back to raw text if not parseable."""
    s = parse_summary(raw)
    if s is None:
        return raw or "(no summary)"

    lines = []
    d = s.get("date") or date
    mc = s.get("message_count", "?")
    tone = s.get("mood", "")
    energy = s.get("energy_level", "")
    initiator = s.get("initiator", "")

    header = f"### {d}  [{tone}, {energy} energy]  ({mc} messages)"
    if score is not None:
        confidence = " (low confidence)" if score < config.FAISS_LOW_CONFIDENCE_THRESHOLD else ""
        header += f"  score={score:.3f}{confidence}"
    lines.append(header)

    if initiator:
        lines.append(f"Initiator: {initiator}")

    topics = s.get("topics", [])
    if topics:
        lines.append(f"Topics: {', '.join(topics)}")

    summary_text = s.get("summary", "")
    if summary_text:
        lines.append("")
        lines.append(summary_text)

    key_moments = s.get("key_moments", [])
    if key_moments:
        lines.append("")
        lines.append("Key moments:")
        for km in key_moments:
            lines.append(f"  - {km}")

    plans = s.get("plans", [])
    if plans:
        lines.append("")
        lines.append("Plans:")
        for p in plans:
            lines.append(f"  - {p}")

    signal = s.get("relationship_signal", "")
    if signal:
        lines.append("")
        lines.append(f"Relationship signal: {signal}")

    return "\n".join(lines)


def format_messages_raw(messages: list[dict]) -> str:
    """Format raw messages for display: timestamp, sender, text/media/transcript."""
    lines = []
    for m in messages:
        ts = m.get("timestamp", "")
        sender = m.get("sender", "")
        header = f"[{ts}] {sender}:"
        parts = [header]
        text = m.get("text")
        if text:
            parts.append(f"  {text}")
        media_type = m.get("media_type")
        if media_type:
            media_file = m.get("media_file", "")
            parts.append(f"  ({media_type}: {media_file})" if media_file else f"  ({media_type})")
        transcript = m.get("transcript")
        if transcript:
            parts.append(f"  [transcript] {transcript}")
        description = m.get("description")
        if description:
            parts.append(f"  [description] {description}")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


# -- Core search functions ----------------------------------------------------

def format_compact(raw: str, date: str = "", score=None) -> str:
    """Single-line scannable summary: date, score, mood, message count, topics, excerpt."""
    s = parse_summary(raw)
    if s is None:
        excerpt = (raw or "")[:150].replace("\n", " ")
        return f"{date}  {excerpt}"

    mood        = s.get("mood", "")
    energy      = s.get("energy_level", "")
    mc          = s.get("message_count", "?")
    topics      = ", ".join(s.get("topics", []))
    summary_txt = (s.get("summary") or "").replace("\n", " ")
    excerpt     = summary_txt[:150] + ("..." if len(summary_txt) > 150 else "")

    score_part = ""
    if score is not None:
        confidence = " (low confidence)" if score < config.FAISS_LOW_CONFIDENCE_THRESHOLD else ""
        score_part = f"  [score: {score:.2f}{confidence}]"

    mood_part   = f"  mood: {mood}" if mood else ""
    energy_part = f", {energy} energy" if energy else ""
    lines = [
        f"{date}{score_part}{mood_part}{energy_part}  msgs: {mc}",
    ]
    if topics:
        lines.append(f"Topics: {topics}")
    if excerpt:
        lines.append(f'"{excerpt}"')
    return "\n".join(lines)


def search_semantic(query: str, top_k: int = 5, threshold: float | None = None):
    """Semantic search over chunk summaries. Returns chunks with parsed summary."""
    threshold = threshold if threshold is not None else config.FAISS_THRESHOLD
    results = search(query, top_k=top_k, threshold=threshold)
    for r in results:
        r["summary_parsed"] = parse_summary(r.get("summary", ""))
    return results


def search_semantic_with_messages(query: str, top_k: int = 5, threshold: float | None = None):
    """Semantic search -- includes raw messages for each result chunk."""
    results = search_semantic(query, top_k=top_k, threshold=threshold)
    for r in results:
        r["messages"] = get_chunk_messages_raw(r["chunk_id"])
    return results


def search_fulltext(query: str, limit: int = 20):
    """Full-text search over message text and notes."""
    from wain.search import _fts_fallback_query
    sql = """
        SELECT m.id, m.timestamp, m.date, m.sender, m.text, m.media_file, m.media_type, m.chunk_id, m.notes
        FROM messages m
        JOIN messages_fts ON messages_fts.rowid = m.id
        WHERE messages_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    """
    with closing(sqlite3.connect(config.active_db_path())) as conn:
        c = conn.cursor()
        for attempt, fts_query in enumerate([query, _fts_fallback_query(query)]):
            try:
                c.execute(sql, (fts_query, limit))
                rows = c.fetchall()
                break
            except sqlite3.OperationalError:
                if attempt == 0:
                    continue
                rows = []
    return [
        {"id": r[0], "timestamp": r[1], "date": r[2], "sender": r[3],
         "text": r[4], "media_file": r[5], "media_type": r[6],
         "chunk_id": r[7], "notes": r[8]}
        for r in rows
    ]


def get_by_date(date: str) -> dict:
    """Get all messages and parsed summary for a specific date."""
    with closing(sqlite3.connect(config.active_db_path())) as conn:
        c = conn.cursor()
        c.execute("SELECT id, summary, notes FROM chunks WHERE date_start = ?", (date,))
        row = c.fetchone()
        chunk = None
        if row:
            chunk = {
                "chunk_id": row[0],
                "summary": row[1],
                "summary_parsed": parse_summary(row[1]),
                "notes": row[2],
            }
        try:
            c.execute("""
                SELECT id, timestamp, sender, text, media_file, media_type, notes, transcript, description
                FROM messages WHERE date = ?
                ORDER BY timestamp ASC
            """, (date,))
            messages = [
                {"id": r[0], "timestamp": r[1], "sender": r[2], "text": r[3],
                 "media_file": r[4], "media_type": r[5], "notes": r[6],
                 "transcript": r[7], "description": r[8]}
                for r in c.fetchall()
            ]
        except sqlite3.OperationalError as e:
            if "no such column" not in str(e):
                raise
            c.execute("""
                SELECT id, timestamp, sender, text, media_file, media_type, notes
                FROM messages WHERE date = ?
                ORDER BY timestamp ASC
            """, (date,))
            messages = [
                {"id": r[0], "timestamp": r[1], "sender": r[2], "text": r[3],
                 "media_file": r[4], "media_type": r[5], "notes": r[6],
                 "transcript": None, "description": None}
                for r in c.fetchall()
            ]
    return {"date": date, "chunk": chunk, "messages": messages}


def get_date_range_summaries(date_from: str, date_to: str):
    """Get parsed chunk summaries for a date range."""
    with closing(sqlite3.connect(config.active_db_path())) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, date_start, message_count, summary, notes
            FROM chunks
            WHERE date_start >= ? AND date_start <= ?
            ORDER BY date_start ASC
        """, (date_from, date_to))
        rows = c.fetchall()
    return [
        {
            "chunk_id": r[0],
            "date": r[1],
            "message_count": r[2],
            "summary": r[3],
            "summary_parsed": parse_summary(r[3]),
            "notes": r[4],
        }
        for r in rows
    ]


def filter_by_date_range(results: list[dict], date_from: str | None, date_to: str | None) -> list[dict]:
    """Filter a results list to chunks whose date_start falls within [date_from, date_to].

    Works on any list of dicts that have a 'date_start' or 'date' key.
    Both bounds are optional and inclusive.
    """
    if not date_from and not date_to:
        return results
    filtered = []
    for r in results:
        d = r.get("date_start") or r.get("date", "")
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        filtered.append(r)
    return filtered


def filter_by_sender(results: list[dict], sender: str) -> list[dict]:
    """Filter results to chunks that contain at least one message from `sender`.

    Matches against raw_sender (real export name) case-insensitively.
    Falls back to sender column if raw_sender is absent.
    """
    if not sender or not results:
        return results
    chunk_ids = [r.get("chunk_id") for r in results if r.get("chunk_id") is not None]
    if not chunk_ids:
        return results
    placeholders = ",".join("?" * len(chunk_ids))
    with closing(sqlite3.connect(config.active_db_path())) as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT chunk_id FROM messages
            WHERE chunk_id IN ({placeholders})
              AND LOWER(COALESCE(raw_sender, sender)) = LOWER(?)
            """,
            chunk_ids + [sender],
        ).fetchall()
    matching = {r[0] for r in rows}
    return [r for r in results if r.get("chunk_id") in matching]


def stats() -> dict:
    """Overall stats about the conversation."""
    with closing(sqlite3.connect(config.active_db_path())) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM messages")
        total, start, end = c.fetchone()
        c.execute("SELECT sender, COUNT(*) FROM messages GROUP BY sender")
        by_sender = dict(c.fetchall())
        c.execute("SELECT media_type, COUNT(*) FROM messages WHERE media_type IS NOT NULL GROUP BY media_type")
        by_media = dict(c.fetchall())
        c.execute("SELECT COUNT(*) FROM chunks WHERE summary IS NOT NULL")
        summarized = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM chunks WHERE embedding_id IS NOT NULL")
        embedded = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM chunks")
        total_chunks = c.fetchone()[0]
    return {
        "total_messages": total,
        "date_range": f"{start} -> {end}",
        "by_sender": by_sender,
        "media_breakdown": by_media,
        "chunks_summarized": f"{summarized}/{total_chunks}",
        "chunks_embedded": f"{embedded}/{total_chunks}",
    }


# -- CLI ----------------------------------------------------------------------

def print_separator():
    print("\n" + "-" * 60 + "\n")


def run_query(
    query: str,
    top_k: int = 5,
    threshold: float | None = None,
    verbose: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
    sender: str | None = None,
):
    """Run a semantic search and print formatted results.

    Falls back to hybrid search if no results meet the threshold, so
    abstract queries never silently return nothing.

    date_from / date_to: inclusive YYYY-MM-DD bounds applied after retrieval.
    sender: filter to chunks containing at least one message from this sender.
    verbose=False (default): compact one-record-per-chunk output.
    verbose=True: full format_summary_markdown() output.
    """
    fmt_fn = format_summary_markdown if verbose else format_compact

    # Browse mode: date range without a query term — return all chunks in range
    if not query and (date_from or date_to):
        _from = date_from or "0000-00-00"
        _to   = date_to   or "9999-99-99"
        results = get_date_range_summaries(_from, _to)
        if sender:
            results = filter_by_sender(results, sender)
        if not results:
            print("No chunks found in that date range.")
            return
        for r in results:
            print(fmt_fn(r.get("summary", ""), date=r.get("date", "")))
            print()
        return

    print(f"\nSearching: {query!r}\n")
    results = search_semantic(query, top_k=top_k, threshold=threshold)

    results = filter_by_date_range(results, date_from, date_to)
    if sender:
        results = filter_by_sender(results, sender)

    if not results:
        from wain.search import hybrid_search
        print("No close semantic matches -- showing hybrid results instead.\n")
        hybrid_results = hybrid_search(query, top_k=top_k)
        hybrid_results = filter_by_date_range(hybrid_results, date_from, date_to)
        if sender:
            hybrid_results = filter_by_sender(hybrid_results, sender)
        if not hybrid_results:
            print("No results found.")
            return
        sep = "-" * 60
        for i, r in enumerate(hybrid_results, 1):
            source_badge = {"semantic": "sem", "keyword": "kw ", "both": "BOTH"}[r["source"]]
            print(
                f"Result {i}/{len(hybrid_results)} [{source_badge}] "
                f"combined={r['combined_score']:.3f}  "
                f"(sem={r['semantic_score']:.3f} kw={r['keyword_score']:.3f})"
            )
            print(fmt_fn(r.get("summary", ""), date=r.get("date_start", "")))
            if verbose:
                print(f"\n{sep}\n")
            else:
                print()
        return

    for i, r in enumerate(results, 1):
        if verbose:
            print(f"Result {i}/{len(results)}:")
        print(fmt_fn(r.get("summary", ""), date=r.get("date_start", ""), score=r["score"]))
        print_separator() if verbose else print()


def interactive(verbose: bool = False):
    """Interactive query REPL.

    verbose=False (default): compact output per result.
    verbose=True: full format_summary_markdown() output.
    Toggle at runtime with 'set verbose on' / 'set verbose off'.
    """
    print("=" * 60)
    print(f"  {SENDER_SELF}/{SENDER_OTHER} Chat Intelligence - Query Interface")
    print("  Commands: 'stats', 'date YYYY-MM-DD', 'hybrid <query>',")
    print("            'set verbose on/off', 'quit'")
    print("  Or type any question for semantic search")
    print(f"  Output mode: {'verbose' if verbose else 'compact'}  (toggle with: set verbose on/off)")
    print("=" * 60)
    print()

    while True:
        try:
            query = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not query:
            continue
        if query.lower() in ("quit", "exit", "q"):
            print("Bye.")
            break
        if query.lower() == "stats":
            print(json.dumps(stats(), indent=2))
            continue

        if query.lower() in ("set verbose on", "verbose on"):
            verbose = True
            print("Output mode: verbose")
            continue
        if query.lower() in ("set verbose off", "verbose off"):
            verbose = False
            print("Output mode: compact")
            continue

        if query.lower().startswith("date "):
            date = query[5:].strip()
            result = get_by_date(date)
            chunk = result.get("chunk")
            if chunk:
                fmt = format_summary_markdown if verbose else format_compact
                print(fmt(chunk["summary"], date=date))
            else:
                print(f"No chunk found for {date}")
            msgs = result.get("messages", [])
            print(f"\n{len(msgs)} messages on this date.")
            continue

        if query.lower().startswith("hybrid "):
            q = query[7:].strip()
            if not q:
                print("Usage: hybrid <query>")
                continue
            print(f"\nHybrid search: {q!r}  "
                  f"(semantic x{config.HYBRID_SEMANTIC_WEIGHT} + keyword x{config.HYBRID_KEYWORD_WEIGHT})\n")
            sep = "-" * 60
            fmt = format_summary_markdown if verbose else format_compact
            results = hybrid_search(q, top_k=5)
            if not results:
                print("No results found.")
            else:
                for i, r in enumerate(results, 1):
                    badge = {"semantic": "sem", "keyword": "kw ", "both": "BOTH"}[r["source"]]
                    print(f"[{i}/{len(results)}] [{badge}] combined={r['combined_score']:.3f}  "
                          f"(sem={r['semantic_score']:.3f} kw={r['keyword_score']:.3f})")
                    print(fmt(r.get("summary", ""), date=r.get("date_start", "")))
                    print(f"\n{sep}\n" if verbose else "")
            continue

        # Parse optional inline flags: --from DATE, --to DATE, --sender NAME
        date_from = date_to = sender_filter = None
        import re as _re
        for flag, attr in [("--from", "date_from"), ("--to", "date_to"), ("--sender", "sender_filter")]:
            m = _re.search(rf"{flag}\s+(\S+)", query)
            if m:
                if attr == "date_from":   date_from = m.group(1)
                elif attr == "date_to":   date_to   = m.group(1)
                else:                     sender_filter = m.group(1)
                query = query[:m.start()].rstrip() + query[m.end():]
        query = query.strip()
        run_query(query, verbose=verbose, date_from=date_from, date_to=date_to, sender=sender_filter)


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args:
        interactive()
    elif args[0] == "--stats":
        print(json.dumps(stats(), indent=2))
    elif args[0] == "--date" and len(args) > 1:
        result = get_by_date(args[1])
        chunk = result.get("chunk")
        if chunk:
            print(format_summary_markdown(chunk["summary"], date=args[1]))
        msgs = result.get("messages", [])
        print(f"\n{len(msgs)} messages.")
    else:
        query = " ".join(args)
        run_query(query)

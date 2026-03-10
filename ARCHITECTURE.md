# Architecture Deep Dive

## The Summarization Pipeline

The core of the system. Getting this right is what separates a useful knowledge base from a glorified grep.

### Why summaries, not raw embeddings?

Raw chat text is noisy. A day with 150 messages might have 80 one-word replies, 40 emoji responses, and 30 messages that actually matter. Embedding the raw text means the semantic index is dominated by noise.

Instead: run a structured LLM pass first. The model reads the full day, extracts what actually happened, and produces a clean structured summary. **That's** what gets embedded. The FAISS index becomes an index of *meaning*, not *words*.

### The self-referential design

```
Chunk N summary generation:

  Input:
  ├── Conversation text (all messages for day N)
  ├── Recent context: summaries of days N-1 and N-2 (always)
  └── Relevant context: top-3 FAISS hits from days 1..N-3
       (only if score ≥ threshold — not always added)

  Output:
  └── Structured JSON summary → stored in DB → embedded → added to FAISS
```

The FAISS retrieval in step 3 is gated: it only fires if the FAISS index has entries (i.e., earlier chunks have already been embedded). Since summarization is sequential, by the time we're on chunk 50, we have 48 embeddings to search. By chunk 10, only 8. By chunk 1, zero — it just uses the sliding window.

This means the system bootstraps naturally without any special-casing.

### Why sequential (not parallel)?

The self-referential context requires strict ordering. Chunk N's summary depends on chunk N-1's summary existing. Parallelizing would break this — chunk 50 might get summarized before chunk 20 has a summary to contribute.

The speed fix is **async API calls**: we don't block the thread while waiting for each OpenAI response. We still process chunks in order, but we're not sitting idle during API latency. Wall time: ~15-25 minutes for 150 chunks vs. ~6+ hours for synchronous blocking calls.

---

## Database Design

### Why SQLite?

- Zero infra overhead — one file, works anywhere
- FTS5 is built-in and fast for keyword search
- Good enough for 16k-100k messages (this use case)
- Easy to inspect, query, and extend

### FTS5 over raw messages

The `messages_fts` virtual table keeps a full-text index over message text and notes, maintained via triggers on insert/update. Useful for:
- Exact phrase search ("Portugal in March")
- Keyword filtering before semantic refinement
- Fast date+keyword combinations

### The notes column

Both `messages` and `chunks` have a `notes` field. This is an annotation layer — human-writable, AI-readable. When a chunk note is updated, `add_note()` automatically clears the `embedding_id` and triggers re-embedding. The FAISS index stays current.

Use cases:
- Tag a chunk: "this is when the dynamic shifted"
- Annotate a message: "they said this but meant the opposite"
- Mark a day: "plans made here were cancelled on 2026-01-15"

---

## FAISS Index

- **Type:** `IndexFlatIP` — inner product (cosine similarity after L2 normalization)
- **Dimension:** 1536 (OpenAI `text-embedding-3-small`)
- **Persistence:** `chat.faiss` + `chat_faiss_meta.json` (maps embedding IDs to chunk IDs)
- **Threshold:** `0.30` default (configurable via `FAISS_THRESHOLD` env var). Hybrid search uses a lower semantic threshold (½ × FAISS_THRESHOLD) since FTS5 keyword scoring compensates for borderline semantic matches.

The metadata file is a simple JSON dict:
```json
{
  "embedding_id_to_chunk_id": {"0": 1, "1": 2, ...},
  "next_id": 32
}
```

FAISS integer IDs (0, 1, 2...) map back to SQLite chunk IDs. The indirection exists because FAISS IDs must be sequential and can't have gaps.

---

## Query Layer

Three access patterns:

| Method | When to use |
|--------|-------------|
| `search_semantic` | "Find days where plans were cancelled" |
| `search_fulltext` | "Find the message where Alice mentioned Portugal" |
| `get_by_date` | "Show me everything from February 7" |

In practice, the most powerful queries combine both: use `search_semantic` to find relevant chunk IDs, then optionally pull full raw messages for those chunks via `get_chunk_messages_raw`.

---

## File Layout

```
wa-chat-intelligence/
├── wain/                    # Python package
│   ├── __init__.py
│   ├── cli.py               # wain CLI entrypoint (Typer)
│   ├── config.py            # All paths/settings from env vars
│   ├── parser.py            # WhatsApp .txt export → SQLite messages
│   ├── chunker.py           # Messages → daily chunks
│   ├── summarizer.py        # Chunks → LLM summaries (sequential async)
│   ├── embedder.py          # Summaries → FAISS index
│   ├── query.py             # Unified query interface (semantic, FTS, date)
│   ├── search.py            # Hybrid search (FAISS + FTS5 combined scoring)
│   ├── transcriber.py       # Voice note transcription via Whisper (local or API)
│   ├── describer.py         # Image description via vision model (OpenAI or compatible)
│   └── schema.py            # Canonical summary JSON schema + validation helpers
│
├── data/                    # Runtime files (gitignored)
│   ├── chat.db              # SQLite database
│   ├── chat.faiss           # FAISS vector index
│   ├── chat_faiss_meta.json # FAISS → chunk ID mapping
│   └── pipeline_state.json  # Last-run timestamps per stage
│
├── tests/                   # Test suite (placeholder)
│
├── scripts/
│   └── migrate_summaries.py  # One-off: backfill canonical schema fields
│
├── pyproject.toml           # Package metadata + wain entry point
├── requirements.txt
├── .env.example             # Documented environment variables
├── README.md
└── ARCHITECTURE.md
```

Install and run:
```bash
source .env
uv pip install -e . --no-build-isolation
wain status
```

---

## Extension Points

**Different chunking strategies:** Daily chunks work well for WhatsApp, but the chunker can be extended to chunk by conversation thread, topic shift (detected via embedding distance), or message count. The `chunks` table schema is already flexible enough.

**Different embedding models:** The embedder abstraction makes it easy to swap `text-embedding-3-small` for a local model (e.g., `sentence-transformers/all-MiniLM-L6-v2`) if API cost is a concern at scale.

**Different summarization models:** The system prompt is model-agnostic. `gpt-5-mini` is the current choice — fast and cheap. For harder analytical queries, swap to a larger model per-chunk without touching the pipeline.

**Media integration:** The parser captures `media_path` for files that exist locally. Audio transcription is fully implemented via `transcriber.py` — Whisper runs on `.opus` voice notes and injects transcripts into chunk text before summarization. Image description is also fully implemented via `describer.py` — a vision model runs on image attachments and injects descriptions as `[Image: <desc>]` into chunk text before summarization. Both media types are first-class citizens in the pipeline with no schema changes required to add further media types.

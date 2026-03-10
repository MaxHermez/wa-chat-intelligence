# wa-chat-intelligence

> Turn a WhatsApp conversation export into a searchable, semantically-indexed knowledge base.

Not a summarizer. Not a chatbot wrapper. A proper intelligence layer on top of a chat history — built to answer deep questions about what was said, when, how, and why.

---

## What It Does

Takes a WhatsApp `.zip` export and builds a structured system with:

- **SQLite + FTS5** — raw message store with full-text search
- **Self-referential summarizer** — LLM summarizes each daily chunk while seeing prior summaries and semantically-relevant earlier context
- **FAISS vector index** — semantic search over summaries (not raw text)
- **Unified query interface** — combine semantic search, keyword search, and date-range retrieval

The result: you can ask "what were the emotional turning points in November?" or "find all times plans were made and then cancelled" — and get back structured, meaningful answers.

---

## Architecture

```
WhatsApp .zip export
        │
        ▼
   [ parser.py ]
   Parses the .txt export line-by-line
   → normalizes senders, timestamps, media refs
   → stores 16k+ messages in SQLite
        │
        ▼
   [ chunker.py ]
   Groups messages into daily chunks
   → one chunk = one day of conversation
   → tags each message with chunk_id
        │
        ▼
   [ summarizer.py ]  ◄─────────────────────────────────┐
   Sequential LLM pass (gpt-5-mini)                     │
   For each chunk (in strict order):                     │
     1. Gets previous 2 summaries (sliding window)       │
     2. Queries FAISS for top-3 relevant earlier         │
        summaries above a similarity threshold           │
        (only if they pass the gate — not always added)  │
     3. Sends: [prior context] + [conversation text]     │
     4. Stores structured JSON summary in DB  ───────────┘
        │
        ▼
   [ embedder.py ]
   Embeds each summary (+ notes if any)
   → text-embedding-3-small via OpenAI API
   → stores vectors in FAISS IndexFlatIP (cosine)
   → maps embedding IDs back to chunk IDs
        │
        ▼
   [ query.py ]
   Unified query layer:
   → semantic_search(query) — FAISS over summaries
   → fulltext_search(query) — FTS5 over raw messages
   → get_by_date(date) — exact day lookup
   → get_date_range_summaries(from, to)
   → stats() — overall conversation analytics
```

---

## Key Design Decisions

### Self-referential summarizer
Each chunk's summary is informed by:
- The **previous 1-2 summaries** (sliding window for continuity)
- **Up to 3 semantically similar earlier summaries** retrieved from FAISS — but only if they cross a similarity threshold and the chunk text suggests prior context is needed

This means a chunk about "the Portugal trip plans" will automatically pull in context from the day those plans were first discussed — without hard-coding any logic about what topics matter.

### Summaries over raw text
Embeddings are computed on **LLM-generated summaries**, not raw chat text. This:
- Abstracts away noise (typos, emoji spam, one-word replies)
- Captures intent and emotional tone, not just words
- Makes semantic search dramatically more useful

### Sequential summarization (no parallelism)
The self-referential design requires strict order. Chunks are summarized one at a time with async API calls — no parallelism. Order is preserved, quality is maintained.

### Notes column
Both `messages` and `chunks` have a `notes` field — a human-writable annotation layer. Chunk notes are automatically re-embedded when updated, keeping the FAISS index current.

---

## Stack

| Component | Tech |
|-----------|------|
| Language | Python 3.12+ |
| Package manager | `uv` |
| Database | SQLite with FTS5 |
| Vector index | FAISS (IndexFlatIP, cosine similarity) |
| Embeddings | OpenAI `text-embedding-3-small` |
| Summarization | OpenAI `gpt-5-mini` |
| Parsing | Custom regex parser for WhatsApp export format |

---

## Getting Your Data

Before running the pipeline, export your WhatsApp chat. Choose "Include media" if you want voice note transcription and image descriptions — without it, only the text messages are available.

> **Note:** Large chats with media can produce multi-GB archives. Make sure you have enough free space before exporting.

### Android

1. Open the chat in WhatsApp
2. Tap the **three-dot menu** (top right) → **More** → **Export chat**
3. Choose **Include media** (recommended) or **Without media**
4. Save or share the resulting `.zip` file

### iOS

1. Open the chat in WhatsApp
2. Tap the **contact name / group name** at the top → **Export Chat**
3. Choose **Attach Media** (recommended) or **Without Media**
4. Save the `.zip` to Files or share it to your computer

### What's in the archive

The `.zip` contains:

- `_chat.txt` — the full chat transcript (set `CHAT_TXT_FILE` to this path)
- Media files (images, voice notes, videos, documents) in the same directory (set `CHAT_EXPORT_DIR` to this directory)

Unzip the archive and point `CHAT_TXT_FILE` and `CHAT_EXPORT_DIR` at the extracted contents before running `wain parse`.

---

## Setup

### 1. Install dependencies

```bash
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

### 2. Set environment variables

```bash
cp .env.example .env
# Edit .env and fill in your values, then:
source .env
```

All paths are read from environment variables with sensible defaults (relative to the project directory). The minimum required variable is `OPENAI_API_KEY`. See `.env.example` for the full list with documentation.

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | _(required)_ | OpenAI API key for summarizer + embedder |
| `CHAT_EXPORT_DIR` | `./export/` | Directory with unzipped WhatsApp export |
| `CHAT_TXT_FILE` | `<CHAT_EXPORT_DIR>/_chat.txt` | Main chat .txt file |
| `DB_PATH` | `./data/chat.db` | SQLite database file |
| `INDEX_PATH` | `./data/chat.faiss` | FAISS vector index |
| `META_PATH` | `./data/chat_faiss_meta.json` | FAISS index metadata |
| `SENDER_SELF` | `Me` | Your canonical display name in output |
| `SENDER_OTHER` | `Them` | The other person's canonical display name |
| `SENDER_SELF_RAW` | _(value of `SENDER_SELF`)_ | Comma-separated raw name(s) from the export that map to you |
| `SUMMARIZER_CONTEXT` | generic description | Free-text context injected into the summarizer prompt — describe who the participants are |
| `WAINTEL_SUMMARIZER_MODEL` | `gpt-5-mini` | LLM model for chunk summarization |
| `WAINTEL_EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI embedding model — ⚠ changing this requires `wain embed --force` (vector dimensions differ) |
| `VISION_BACKEND` | `api` | Image description backend — `api` or `none` (skip without error) |
| `VISION_MODEL` | `gpt-5.2` | Vision model for image descriptions |

### 3. Install the CLI

```bash
uv pip install -e . --no-build-isolation
```

This installs the `wain` command into your virtualenv. After this, you can use `wain` instead of running scripts directly.

### 4. Check pipeline state

```bash
wain status
```

### 5. Quickstart: run the full pipeline

```bash
wain run
# Skip optional media stages if you don't need them:
wain run --skip-transcribe --skip-describe
# With a named workspace:
wain run --workspace alice
```

Runs all stages in order: parse → transcribe → describe → chunk → summarize → embed.
Each stage is delta-aware — safe to re-run on an existing workspace.

### 6. Parse the export (individual stages)

```bash
wain parse
# or directly: python parser.py
```

Parses the WhatsApp `.txt` export into SQLite. Outputs message count, date range, and sender breakdown.

### 7. Transcribe voice notes (optional)

```bash
wain transcribe
# Choose backend: wain transcribe --backend api
# Limit for testing: wain transcribe --limit 10
```

Runs Whisper on all `.opus` audio messages and stores transcripts in the DB.
Skip this step if you have no audio files or don't need voice note content.
Idempotent — already-transcribed messages are always skipped.

### 8. Chunk messages

```bash
wain chunk
# or directly: python chunker.py
```

Groups messages into daily chunks. One chunk = one day.

### 9. Summarize

```bash
wain summarize
# Resume from a specific chunk: wain summarize --from 42
# Dry run (no API calls): wain summarize --dry-run
# or directly: python summarizer.py
```

Runs the sequential LLM summarization pass. Async API calls keep it fast without breaking ordering. Expect ~15-25 minutes for 150+ chunks.

### 10. Embed

```bash
wain embed
# or directly: python embedder.py
```

Embeds all summarized chunks into the FAISS index. Run this after summarization completes (or incrementally after each batch).

### 11. Query

```bash
# Interactive REPL
wain query

# One-shot semantic search
wain query "plans that got cancelled"

# Full-text keyword search
wain query "Portugal" --fulltext

# Look up a specific date
wain query --date 2025-12-25

# Stats
wain query --stats

# Python API (still works)
# from query import search_semantic, search_fulltext, get_by_date, stats
```

---

## Data Model

### `messages`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| timestamp | TEXT | ISO 8601 |
| date | TEXT | YYYY-MM-DD |
| sender | TEXT | Normalized sender name |
| raw_sender | TEXT | Original name from export |
| text | TEXT | Message body (null for media-only) |
| media_file | TEXT | Filename if media attached |
| media_type | TEXT | image / video / audio / document / other |
| media_path | TEXT | Resolved local path (if file exists) |
| chunk_id | INTEGER | FK → chunks.id |
| notes | TEXT | Human annotation |
| transcript | TEXT | Whisper transcription for audio messages |

### `chunks`
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| date_start | TEXT | YYYY-MM-DD |
| date_end | TEXT | YYYY-MM-DD (same as start for daily chunks) |
| msg_start_id | INTEGER | First message ID in chunk |
| msg_end_id | INTEGER | Last message ID in chunk |
| message_count | INTEGER | Messages in this chunk |
| summary | TEXT | JSON-structured LLM summary |
| embedding_id | INTEGER | Index in FAISS (null if not yet embedded) |
| notes | TEXT | Human annotation (triggers re-embedding on update) |

### Summary JSON structure
Each chunk summary is stored as structured JSON:
```json
{
  "date": "YYYY-MM-DD",
  "message_count": 87,
  "energy_level": "high",
  "mood": "warm",
  "initiator": "Alice",
  "topics": ["travel plans", "work stress", "cooking"],
  "key_moments": ["Alice asked about the Portugal dates", "Bob mentioned missing her"],
  "plans": ["Portugal late March"],
  "cancellations": [],
  "media_context": "3 voice notes exchanged, tone was warm",
  "relationship_signal": "high engagement from both sides, planning mode",
  "needs_prior_context": false,
  "summary": "..."
}
```

---

## Current State

| Stage | Status |
|-------|--------|
| Parser | ✅ Complete |
| Transcriber | ✅ Optional — voice notes via Whisper |
| Chunker | ✅ Complete |
| Summarizer | ✅ Complete |
| Embedder | ✅ Complete |
| Query layer | ✅ Complete |

---

## TODO

- [x] Externalize all hardcoded paths to `config.py` + `.env.example`
- [x] Make sender normalization configurable via `SENDER_SELF` / `SENDER_OTHER` / `SENDER_SELF_RAW`
- [x] Unified CLI: `wain` command with parse/chunk/summarize/embed/query/status subcommands
- [x] Resume-safe summarization (already partial, needs better checkpointing)
- [ ] Embedder: batch API calls to reduce cost on large backlogs
- [x] Query layer: hybrid search (semantic + FTS combined scoring)
- [x] Media analysis: transcript audio via Whisper (describe images inline — future)
- [ ] Web UI or simple REPL for interactive querying
- [ ] Tests

---

## Multiple Conversations

wain supports fully isolated workspaces — one per conversation (partner, family group, etc).

### Create workspaces

```bash
wain init alice
wain init family
```

Each workspace gets its own directory at `~/.wain/workspaces/<name>/` containing an isolated `chat.db`, FAISS index, and pipeline state.

### Run the full pipeline per workspace

```bash
# Point at the export, then run each stage with --workspace
export CHAT_TXT_FILE=/path/to/alice-export/_chat.txt
wain parse      --workspace alice
wain transcribe --workspace alice
wain chunk      --workspace alice
wain summarize  --workspace alice
wain embed      --workspace alice

# Query it
wain query "when did they first meet" --workspace alice
wain status --workspace alice
```

All subcommands accept `--workspace`. You can also set `WAINTEL_WORKSPACE=alice` in your environment to avoid typing it on every command.

### List workspaces

```bash
wain list
```

Shows all workspaces with a quick stats summary (messages, chunks, summarized, embedded).

### Custom workspace root

```bash
export WAINTEL_WORKSPACE_ROOT=/mnt/data/wain-workspaces
```

### Single-conversation users

No change — the `--workspace` flag is optional. Omit it and wain behaves exactly as before, using the paths in `config.py` / `.env`.


## Incremental Updates

Every pipeline stage is delta-aware. To add new messages from a fresh WhatsApp export, just run the full pipeline again — each stage processes only what's new:

```bash
wain parse       # inserts only messages newer than what's already in the DB
wain transcribe  # transcribes only audio messages where transcript IS NULL
wain describe    # describes images where description IS NULL (VISION_BACKEND=none to skip)
wain chunk       # creates new chunks for new dates; extends the boundary chunk if it grew
wain summarize   # summarizes only chunks with summary IS NULL
wain embed       # embeds only chunks with embedding_id IS NULL
```

### How each stage handles incremental runs

| Stage | Behavior on re-run |
|---|---|
| `parse` | Finds `MAX(timestamp)` in DB; inserts only messages after that point. Deduplicates at the boundary by `(timestamp, sender, text)`. |
| `transcribe` | Skips messages where `transcript IS NOT NULL`. Only processes new audio messages. |
| `transcribe` (no file) | Gracefully skips audio messages where the file no longer exists on disk. |
| `chunk` | Skips dates already fully chunked. Extends the boundary chunk (last date) if new messages arrived; clears its `summary` and `embedding_id` so it gets re-processed. |
| `summarize` | Always skips chunks where `summary IS NOT NULL`. Runs only on new/extended chunks. |
| `embed` | Always skips chunks where `embedding_id IS NOT NULL`. Runs only after summarize adds new summaries. |

### The boundary chunk

The last chunk in the DB may be a partial day if the export was taken mid-day. When new messages arrive for that same date, the chunk is **extended** (not duplicated) and its summary is cleared for re-summarization. All other completed chunks are untouched.

### Running twice on the same export

Fully idempotent — running the pipeline twice on an unchanged export produces zero changes at every stage.


## Privacy

This tool processes private conversation data. The `.db`, `.faiss`, and export files are gitignored. Never commit them. The code is generic — the data is yours.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**wa-chat-intelligence** (`wain`) is a CLI tool that ingests WhatsApp chat exports and builds a searchable, LLM-summarized knowledge base. It parses `.txt` exports into SQLite, chunks messages by day, generates structured summaries via OpenAI, embeds them into a FAISS index, and exposes semantic + full-text + date-based queries.

## Workflow Rules

- **Always `git pull` before starting any work** — another agent may have pushed changes. Pull frequently during long sessions too, especially before reading or editing files.
- **Never fix code directly** — always file issues to Linear instead. A coding agent automatically picks them up. The only exception is editing `CLAUDE.md` itself.
- **Issue tracker**: Linear, team `HHC`, project `wa-chat-intelligence`. File issues there, not in markdown files.
- **Don't use personal names** in code, docs, or examples. Use Alice/Bob as generic names (consistent with `.env.example`).
- **Windows compatibility**: Avoid Unicode symbols (checkmarks, em-dashes, etc.) in `print()` output — Windows cp1252 encoding can't handle them. Use ASCII alternatives.

## Build & Run

```bash
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt
uv pip install -e . --no-build-isolation
```

Requires `OPENAI_API_KEY` env var. See `.env.example` for all config options.

## CLI Commands (pipeline order)

```bash
wain status                # Pipeline state & consistency checks
wain parse                 # WhatsApp .txt → SQLite (delta-aware)
wain transcribe            # Voice notes → text via Whisper (optional)
wain chunk                 # Messages → daily chunks (delta-aware)
wain summarize             # Chunks → LLM summaries (sequential, async)
wain embed                 # Summaries → FAISS index (delta-aware)
wain query                 # Interactive search (semantic/full-text/date)
wain validate              # Check summaries against canonical schema
wain init <name>           # Create isolated workspace
wain list                  # List workspaces
```

All commands accept `--workspace <name>` for multi-conversation support.

## Testing

No test framework is configured yet. The `/tests/` directory is empty. Validation can be run via `wain validate`.

## Architecture

### Pipeline Flow
Parse → (Transcribe) → (Describe) → Chunk → Summarize → Embed → Query

Each stage is **delta-aware** — it tracks what was last processed via `data/pipeline_state.json` and only processes new data.

### Key Modules (`wain/`)
- **cli.py** — Typer app, all commands defined here. Entry point: `wain.cli:app`
- **config.py** — All configuration from env vars with defaults. Resolves workspace-aware paths
- **schema.py** — Canonical `ChunkSummary` JSON schema and validation
- **parser.py** — Regex-based WhatsApp export parser → SQLite `messages` table
- **chunker.py** — Groups messages into daily chunks in `chunks` table
- **summarizer.py** — Sequential async OpenAI calls (gpt-5-mini). Uses sliding window of prior summaries + FAISS-retrieved context for self-referential continuity
- **embedder.py** — OpenAI text-embedding-3-small → FAISS IndexFlatIP (cosine similarity via L2-normed inner product)
- **query.py** — Unified query interface across all access patterns
- **search.py** — Hybrid search: weighted combination of FAISS semantic scores + FTS5 BM25 keyword scores (default 0.7/0.3)
- **transcriber.py** — Whisper transcription (local CLI or OpenAI API)
- **describer.py** — Image description via OpenAI vision API (GPT-5.2)

### Database (SQLite)
- **messages** — Parsed messages with sender normalization, media metadata, optional transcript
- **chunks** — Daily message groups with JSON summary and FAISS embedding_id
- **messages_fts / chunks_fts** — FTS5 virtual tables for full-text search

### FAISS Index
- Dimension: 1536, type: IndexFlatIP
- Metadata mapping (embedding ID → chunk ID) in `chat_faiss_meta.json`
- Similarity threshold: 0.30 (configurable via `FAISS_THRESHOLD` env var)

### Summarization Design
Summaries are generated **sequentially** (not parallel) because each summary can reference prior context. The summarizer injects:
1. Previous 1-2 summaries (sliding window)
2. Up to 3 semantically similar earlier summaries from FAISS (if score ≥ threshold)

### Data Storage
- Default: `./data/` (git-ignored) for single-conversation use
- Workspaces: `~/.wain/workspaces/<name>/` for multi-conversation isolation

## Key Environment Variables

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Required for summarization, embedding, API transcription |
| `CHAT_EXPORT_DIR` | Path to unzipped WhatsApp export (default: `./export/`) |
| `SENDER_SELF` / `SENDER_OTHER` | Display names for output |
| `SENDER_SELF_RAW` | Comma-separated raw names from export to map to SENDER_SELF |
| `SUMMARIZER_CONTEXT` | Free-text context injected into summarizer prompt |
| `FAISS_THRESHOLD` | Cosine similarity gate (default: 0.30) |
| `WHISPER_BACKEND` | "local" or "api" (default: "local") |

## Dependencies

- **openai** — LLM summaries, embeddings, Whisper API
- **faiss-cpu** — Vector similarity search
- **numpy** — Embedding normalization
- **python-dateutil** — Date parsing (dayfirst=True)
- **typer** — CLI framework

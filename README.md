# wain

Turn a WhatsApp chat export into a searchable, semantically-indexed knowledge base.

```bash
pip install wain
```

---

## What it does

Takes a WhatsApp `.zip` export and builds:

- **SQLite + FTS5** full-text search over raw messages
- **Daily LLM summaries** that reference prior context automatically
- **FAISS vector index** for semantic search over summaries
- **Unified query interface** combining semantic, keyword, and date-range search

```
Parse → Transcribe → Describe → Chunk → Summarize → Embed → Query
```

Each stage is delta-aware — safe to re-run when new messages arrive.

---

## Quickstart

```bash
# 1. Configure
wain config set openai-api-key sk-...
wain config set chat-txt-file /path/to/export/_chat.txt

# 2. Run the full pipeline
wain run

# 3. Query
wain query "plans that got cancelled"
wain query "Portugal" --fulltext
wain query --date 2025-12-25
wain query --stats
```

That's it. `wain status` shows pipeline progress at any time.

---

## Getting your data

Export your WhatsApp chat before running the pipeline.

**Android:** Open chat → three-dot menu → More → Export chat → Include media → save `.zip`

**iOS:** Open chat → tap contact name → Export Chat → Attach Media → save `.zip`

Unzip and point `chat-txt-file` at the `_chat.txt` inside.

---

## Multiple conversations

```bash
wain init alice
wain run --workspace alice
wain query "weekend plans" --workspace alice
```

Each workspace gets its own database, index, and config at `~/.wain/workspaces/<name>/`.

---

## Configuration

The minimum config is an OpenAI API key and a path to your chat export. Everything else has sensible defaults.

```bash
wain config set openai-api-key sk-...    # stored in OS keyring
wain config set sender-self Alice         # your display name
wain config set sender-other Bob          # the other person
wain config show                          # see all settings + where they come from
```

Settings are resolved in order: **CLI flags → workspace config → global config → env vars**.

See [CONFIGURATION.md](CONFIGURATION.md) for the full reference.

---

## How it works

Messages are grouped into **daily chunks**, then each chunk is summarized by an LLM that sees:

1. The previous 1-2 summaries (sliding window)
2. Up to 3 semantically similar earlier summaries from FAISS

This means a chunk about "the Portugal trip" automatically pulls in context from when those plans were first discussed — no hard-coded topic logic.

Embeddings are computed on **summaries, not raw text** — this filters out noise (typos, emoji, one-word replies) and makes semantic search significantly more useful.

---

## Stack

| Component | Tech |
|-----------|------|
| Language | Python 3.12+ |
| Database | SQLite + FTS5 |
| Vector index | FAISS (cosine similarity) |
| Embeddings | OpenAI `text-embedding-3-small` |
| Summarization | OpenAI `gpt-5-mini` |
| CLI | Typer |

---

## Privacy

This tool processes private conversation data. Database, index, and export files are gitignored and never leave your machine. The code is generic — the data is yours.

---

## License

MIT

# Configuration Reference

wain resolves settings in this order (highest priority first):

1. **CLI flags** (`--sender-self`, `--threshold`, etc.)
2. **Workspace config** (`~/.wain/workspaces/<name>/config.toml`)
3. **Global config** (`~/.wain/config.toml`)
4. **Environment variables** (lowest priority fallback)

Use `wain config show` to see all effective values and where each one comes from.

---

## Setting values

```bash
# Global config (applies to all workspaces)
wain config set <key> <value>

# Per-workspace config
wain config set <key> <value> --workspace alice

# View current config
wain config show
wain config show --workspace alice
```

---

## Config keys

### Required

| Key | Env var | Description |
|-----|---------|-------------|
| `openai-api-key` | `OPENAI_API_KEY` | OpenAI API key. Stored in OS keyring when possible, falls back to config file. |

### Chat export

| Key | Env var | Default | Description |
|-----|---------|---------|-------------|
| `chat-export-dir` | `CHAT_EXPORT_DIR` | `./export/` | Directory containing the unzipped WhatsApp export |
| `chat-txt-file` | `CHAT_TXT_FILE` | `<chat-export-dir>/_chat.txt` | Path to the main chat `.txt` file |

### Sender identity

| Key | Env var | Default | Description |
|-----|---------|---------|-------------|
| `sender-self` | `SENDER_SELF` | `Me` | Your display name in output |
| `sender-other` | `SENDER_OTHER` | `Them` | The other person's display name |
| `sender-self-raw` | `SENDER_SELF_RAW` | value of `sender-self` | Comma-separated raw name(s) from the export that map to you (case-insensitive) |
| `summarizer-context` | `SUMMARIZER_CONTEXT` | auto-generated | Free-text context injected into the summarizer prompt — describe who the participants are |

---

## Environment-only settings

These are configured via environment variables only (not available through `wain config set`).

### Database and index paths

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `./data/chat.db` | SQLite database file |
| `INDEX_PATH` | `./data/chat.faiss` | FAISS vector index |
| `META_PATH` | `./data/chat_faiss_meta.json` | FAISS index metadata |
| `STATE_PATH` | `./data/pipeline_state.json` | Pipeline state tracking |

### Search tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `FAISS_THRESHOLD` | `0.20` | Cosine similarity gate for semantic search |
| `FAISS_LOW_CONFIDENCE_THRESHOLD` | `0.30` | Scores below this show a "low confidence" label |
| `WAINTEL_HYBRID_SEMANTIC_WEIGHT` | `0.7` | Weight for semantic score in hybrid search |
| `WAINTEL_HYBRID_KEYWORD_WEIGHT` | `0.3` | Weight for keyword score in hybrid search |

### Models

| Variable | Default | Description |
|----------|---------|-------------|
| `WAINTEL_SUMMARIZER_MODEL` | `gpt-5-mini` | LLM for chunk summarization |
| `WAINTEL_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model. Changing this requires `wain embed --force`. |
| `VISION_BACKEND` | `api` | Image description backend: `api` or `none` |
| `VISION_MODEL` | `gpt-5.2` | Vision model for image descriptions |

### Transcription

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_BACKEND` | `local` | `local` (Whisper CLI) or `api` (OpenAI API) |
| `WHISPER_MODEL` | `base` | Model size: `tiny`, `base`, `small`, `medium`, `large` (local) or `whisper-1` (API) |
| `WHISPER_LANGUAGE` | _(auto-detect)_ | ISO 639-1 language hint (e.g. `en`, `ru`, `de`) |

### Date parsing

| Variable | Default | Description |
|----------|---------|-------------|
| `WAINTEL_DATE_DAYFIRST` | `true` | `true` for DD/MM/YYYY (most regions), `false` for US MM/DD/YYYY exports |

### Workspaces

| Variable | Default | Description |
|----------|---------|-------------|
| `WAINTEL_WORKSPACE` | _(none)_ | Default workspace name (equivalent to `--workspace` on every command) |
| `WAINTEL_WORKSPACE_ROOT` | `~/.wain/workspaces/` | Root directory for all workspaces |

---

## Config file format

Global config (`~/.wain/config.toml`):

```toml
[defaults]
sender_self = "Alice"
sender_other = "Bob"
sender_self_raw = "Alice,Al"
chat_export_dir = "/path/to/export"
chat_txt_file = "/path/to/export/_chat.txt"

[openai]
api_key = "sk-..."   # only used if keyring is unavailable
```

Workspace config (`~/.wain/workspaces/<name>/config.toml`):

```toml
[workspace]
sender_self = "Alice"
sender_other = "Bob"
sender_self_raw = "Alice"
chat_export_dir = "/path/to/alice-export"
chat_txt_file = "/path/to/alice-export/_chat.txt"
```


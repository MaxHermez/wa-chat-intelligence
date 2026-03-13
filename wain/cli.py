"""
cli.py -- unified wain CLI for wa-chat-intelligence.

Commands (run in this order for a fresh export):
  wain status      Show pipeline state, consistency checks, last-run timestamps
  wain parse       Parse WhatsApp .txt export -> SQLite messages
  wain chunk       Group messages into daily chunks
  wain summarize   LLM-summarize all unsummarized chunks (OpenAI)
  wain embed       Embed chunk summaries into FAISS index (OpenAI)
  wain query       Interactive or one-shot semantic/FTS query

Install once after cloning:
  uv pip install -e . --no-build-isolation
Then run: wain --help
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
import time

# Reconfigure stdout to UTF-8 so LLM-generated Unicode in summaries doesn't
# crash on Windows cp1252 consoles. errors='replace' substitutes ? for any
# codepoint the terminal truly can't handle rather than raising UnicodeEncodeError.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from typing import Annotated

from wain import __version__, config

# -- Typer app -----------------------------------------------------------------

# -- Config subapp ------------------------------------------------------------

config_app = typer.Typer(
    name="config",
    help="Manage wain configuration (global and per-workspace).",
    no_args_is_help=True,
)

# Valid settable keys and where they live
_CONFIG_KEYS = {
    # key                section          scope
    "openai-api-key":    ("openai",       "global"),
    "sender-self":       ("workspace",    "both"),
    "sender-other":      ("workspace",    "both"),
    "sender-self-raw":   ("workspace",    "both"),
    "chat-txt-file":     ("workspace",    "both"),
    "chat-export-dir":   ("workspace",    "both"),
    "summarizer-context":("workspace",    "both"),
}

_KEY_TO_TOML = {
    "openai-api-key":     ("openai",     "api_key"),
    "sender-self":        ("workspace",  "sender_self"),
    "sender-other":       ("workspace",  "sender_other"),
    "sender-self-raw":    ("workspace",  "sender_self_raw"),
    "chat-txt-file":      ("workspace",  "chat_txt_file"),
    "chat-export-dir":    ("workspace",  "chat_export_dir"),
    "summarizer-context": ("workspace",  "summarizer_context"),
}


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help=f"Config key. Valid: {', '.join(_CONFIG_KEYS)}")],
    value: Annotated[str, typer.Argument(help="Value to set.")],
    workspace: WorkspaceOpt = None,
    global_: Annotated[bool, typer.Option("--global", "-g", help="Write to global config even for workspace-capable keys.")] = False,
):
    """Set a config value in ~/.wain/config.toml (global) or a workspace config.toml."""
    if key not in _KEY_TO_TOML:
        typer.echo(f"Unknown key: {key!r}. Valid keys: {', '.join(_CONFIG_KEYS)}", err=True)
        raise typer.Exit(1)

    scope = _CONFIG_KEYS[key][1]

    # openai-api-key: try keyring first, fall back to config file
    if key == "openai-api-key":
        try:
            import keyring as _kr
            _kr.set_password("wain", "openai_api_key", value)
            typer.echo("Stored openai-api-key in system keyring.")
            return
        except Exception:
            pass
        # Keyring unavailable — write to global config file
        data = config.get_global_config()
        data.setdefault("openai", {})["api_key"] = value
        config.save_global_config(data)
        typer.echo(f"Stored openai-api-key in {config._GLOBAL_CONFIG_PATH}")
        return

    section, field = _KEY_TO_TOML[key]

    if workspace and not global_:
        # Write to workspace config
        data = config.get_workspace_config(workspace)
        data.setdefault(section, {})[field] = value
        config.save_workspace_config(workspace, data)
        typer.echo(f"Set {key} = {value!r} in workspace '{workspace}'")
    else:
        # Write to global config [defaults] section
        data = config.get_global_config()
        if section == "workspace":
            section = "defaults"
        data.setdefault(section, {})[field] = value
        config.save_global_config(data)
        typer.echo(f"Set {key} = {value!r} in global config")


@config_app.command("show")
def config_show(
    workspace: WorkspaceOpt = None,
):
    """Show current effective config with source attribution for each value."""
    # Activate workspace so _resolve() uses the right layers
    if workspace:
        config.set_workspace(workspace)

    typer.echo("Config resolution: CLI args > workspace TOML > global TOML > env vars")
    typer.echo(f"Global config:    {config._GLOBAL_CONFIG_PATH}")
    if workspace:
        typer.echo(f"Workspace config: {config._WAIN_HOME / 'workspaces' / workspace / 'config.toml'}")
    typer.echo()

    sources = config.resolve_sources()

    # Display resolvable keys with source
    typer.echo("Effective values:")
    max_key = max(len(k) for k in sources)
    for key, (val, src) in sorted(sources.items()):
        display_val = val if val else "(not set)"
        typer.echo(f"  {key:<{max_key}}  {display_val}  ({src})")

    # Non-resolvable keys (env-only tuning knobs)
    typer.echo()
    typer.echo("Tuning (env vars only):")
    typer.echo(f"  faiss_threshold        {config.FAISS_THRESHOLD}")
    typer.echo(f"  low_confidence_thresh  {config.FAISS_LOW_CONFIDENCE_THRESHOLD}")
    typer.echo(f"  hybrid_semantic_weight {config.HYBRID_SEMANTIC_WEIGHT}")
    typer.echo(f"  hybrid_keyword_weight  {config.HYBRID_KEYWORD_WEIGHT}")
    typer.echo(f"  summarizer_model       {config.SUMMARIZER_MODEL}")
    typer.echo(f"  embedding_model        {config.EMBEDDING_MODEL}")
    typer.echo(f"  vision_model           {config.VISION_MODEL}")


def _version_callback(value: bool) -> None:
    if value:
        print(f"wain {__version__}")
        raise typer.Exit()


app = typer.Typer(
    name="wain",
    help=(
        "wa-chat-intelligence -- turn a WhatsApp export into a searchable knowledge base.\n\n"
        "Run steps in order: parse -> chunk -> summarize -> embed -> query\n\n"
        "Run [bold]wain status[/bold] at any time to see pipeline state."
    ),
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.callback()
def main(
    version: Annotated[
        Optional[bool],
        typer.Option("--version", "-V", help="Show version and exit.", callback=_version_callback, is_eager=True),
    ] = None,
) -> None:
    pass
app.add_typer(config_app, name="config")


# -- Progress helper -----------------------------------------------------------

def _step(msg: str) -> None:
    """Print a progress step to stderr so it's always visible even when stdout is piped."""
    typer.echo(f"  {msg}", err=True)



# -- Workspace helpers ---------------------------------------------------------

WorkspaceOpt = Annotated[
    Optional[str],
    typer.Option(
        "--workspace", "-w",
        envvar="WAINTEL_WORKSPACE",
        help=(
            "Workspace name. Uses ~/.wain/workspaces/<name>/ for all data files. "
            "Reads WAINTEL_WORKSPACE env var if not passed. "
            "Omit to use single-conversation defaults from config.py."
        ),
    ),
]


def _apply_workspace(name: Optional[str]) -> None:
    """Activate a named workspace (or reset to defaults if None)."""
    if name:
        from pathlib import Path as _Path
        ws_paths = config.get_workspace_paths(name)
        if not _Path(ws_paths.root).exists():
            typer.echo(
                f"Workspace '{name}' not found. Create it with: wain init {name}",
                err=True,
            )
            raise typer.Exit(1)
        ws = config.set_workspace(name)
        if not ws:
            _abort(f"Failed to activate workspace '{name}'.")
    else:
        config.set_workspace(None)


# -- DB helpers ----------------------------------------------------------------

def _db_connect() -> sqlite3.Connection:
    return sqlite3.connect(config.active_db_path())


def _db_count(query: str, params: tuple = ()) -> int:
    try:
        with closing(_db_connect()) as conn:
            return conn.execute(query, params).fetchone()[0]
    except Exception:
        return 0


def _msg_count() -> int:
    return _db_count("SELECT COUNT(*) FROM messages")


def _chunk_count() -> int:
    return _db_count("SELECT COUNT(*) FROM chunks")


def _summary_count() -> int:
    return _db_count("SELECT COUNT(*) FROM chunks WHERE summary IS NOT NULL")


def _embedded_count() -> int:
    return _db_count("SELECT COUNT(*) FROM chunks WHERE embedding_id IS NOT NULL")


# -- FAISS helpers -------------------------------------------------------------

def _load_meta() -> dict:
    """Load FAISS metadata JSON. Returns empty structure if file doesn't exist."""
    if not os.path.exists(config.active_meta_path()):
        return {"embedding_id_to_chunk_id": {}, "next_id": 0}
    try:
        with open(config.active_meta_path()) as f:
            return json.load(f)
    except Exception:
        return {"embedding_id_to_chunk_id": {}, "next_id": 0}


def _faiss_vector_count() -> int:
    try:
        import faiss
        if not os.path.exists(config.active_index_path()):
            return 0
        idx = faiss.read_index(config.active_index_path())
        return idx.ntotal
    except Exception:
        return 0


# -- Pipeline state (timestamps) -----------------------------------------------

def _load_state() -> dict:
    """Load pipeline_state.json. Returns empty dict if missing."""
    if not os.path.exists(config.active_state_path()):
        return {}
    try:
        with open(config.active_state_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _record_run(stage: str, **extra) -> None:
    """Record a successful stage completion with UTC timestamp."""
    state = _load_state()
    state[stage] = {
        "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **extra,
    }
    try:
        with open(config.active_state_path(), "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass  # non-fatal


# -- Guard helpers -------------------------------------------------------------

def _abort(msg: str) -> None:
    typer.echo(f"[error] {msg}", err=True)
    raise typer.Exit(code=1)


def _require_messages() -> None:
    if _msg_count() == 0:
        _abort("No messages in database. Run 'wain parse' first.")


def _require_chunks() -> None:
    if _chunk_count() == 0:
        _abort("No chunks in database. Run 'wain chunk' first.")


def _require_summaries() -> None:
    if _summary_count() == 0:
        _abort("No summaries in database. Run 'wain summarize' first.")


def _require_openai() -> None:
    if not config.OPENAI_API_KEY:
        _abort("OPENAI_API_KEY is not set. Run 'wain config set openai-api-key <key>' or set the OPENAI_API_KEY env var.")


def _require_index() -> None:
    if _faiss_vector_count() == 0:
        _abort("FAISS index is empty or missing. Run 'wain embed' first.")


# -- status --------------------------------------------------------------------

@app.command()
def status(
    workspace: WorkspaceOpt = None,
    fix: Annotated[bool, typer.Option("--fix", help="Repair auto-fixable inconsistencies (clears stale embedding_ids).")] = False,
):
    """
    Show pipeline state, consistency checks, and last-run timestamps.

    Runs in-process checks only -- no API calls, completes in under 3 seconds.
    Use --fix to repair orphaned embedding_id values without data loss.
    """
    _apply_workspace(workspace)
    t0 = time.monotonic()
    issues: list[str] = []

    typer.echo("wa-chat-intelligence -- pipeline status\n")

    # -- Config ----------------------------------------------------------------
    typer.echo("Config:")
    typer.echo(f"  DB:         {config.active_db_path()}")
    typer.echo(f"  Index:      {config.active_index_path()}")
    typer.echo(f"  Meta:       {config.active_meta_path()}")
    typer.echo(f"  State:      {config.active_state_path()}")
    typer.echo(f"  Export:     {config.CHAT_TXT_FILE}")
    typer.echo(f"  Sender:     {config.SENDER_SELF} / {config.SENDER_OTHER}")
    typer.echo(f"  OpenAI key: {'set' if config.OPENAI_API_KEY else 'NOT SET (!)'}")
    typer.echo()

    # -- DB checks -------------------------------------------------------------
    _step("Checking DB...")
    db_exists = Path(config.active_db_path()).exists()

    msgs = 0
    chunks = 0
    summaries = 0
    embedded_in_db = 0
    db_embedding_ids: set[int] = set()
    db_chunk_ids: set[int] = set()
    date_range = ("--", "--")

    if db_exists:
        try:
            conn = _db_connect()
            msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            summaries = conn.execute("SELECT COUNT(*) FROM chunks WHERE summary IS NOT NULL").fetchone()[0]

            rows = conn.execute("SELECT id, embedding_id FROM chunks WHERE embedding_id IS NOT NULL").fetchall()
            for chunk_id, emb_id in rows:
                db_embedding_ids.add(emb_id)
                db_chunk_ids.add(chunk_id)
            embedded_in_db = len(rows)

            r = conn.execute("SELECT MIN(date), MAX(date) FROM messages").fetchone()
            if r and r[0]:
                date_range = (r[0], r[1])

            # Duplicate embedding_id check
            dup_rows = conn.execute("""
                SELECT embedding_id, COUNT(*) as cnt
                FROM chunks
                WHERE embedding_id IS NOT NULL
                GROUP BY embedding_id
                HAVING cnt > 1
            """).fetchall()
            if dup_rows:
                for emb_id, cnt in dup_rows:
                    issues.append(f"Duplicate embedding_id {emb_id} assigned to {cnt} chunks.")

        except Exception as e:
            issues.append(f"DB read error: {e}")

    # -- FAISS checks ----------------------------------------------------------
    _step("Checking FAISS index...")
    index_exists = Path(config.active_index_path()).exists()
    meta_exists = Path(config.active_meta_path()).exists()
    vectors = 0
    meta = _load_meta()
    meta_chunk_ids: set[int] = set()

    if index_exists:
        try:
            import faiss
            idx = faiss.read_index(config.active_index_path())
            vectors = idx.ntotal
        except Exception as e:
            issues.append(f"FAISS read error: {e}")

    if meta_exists:
        meta_chunk_ids = {int(v) for v in meta["embedding_id_to_chunk_id"].values()}
        next_id = meta.get("next_id", 0)

        # Check: index size matches next_id
        if index_exists and vectors != next_id:
            issues.append(
                f"FAISS index has {vectors} vectors but meta.next_id = {next_id}. "
                "Index and meta are out of sync."
            )

    # -- Consistency checks ----------------------------------------------------
    _step("Checking consistency...")

    orphaned_db_chunks: list[int] = []   # chunks with embedding_id not in meta
    orphaned_meta_entries: list[int] = [] # meta chunk_ids not in DB

    if db_exists and meta_exists:
        meta_emb_keys = {int(k) for k in meta["embedding_id_to_chunk_id"].keys()}

        # 1. Every chunk.embedding_id must exist as a key in meta
        for emb_id in db_embedding_ids:
            if emb_id not in meta_emb_keys:
                # Find which chunk this belongs to
                with closing(_db_connect()) as conn:
                    row = conn.execute("SELECT id FROM chunks WHERE embedding_id = ?", (emb_id,)).fetchone()
                if row:
                    orphaned_db_chunks.append(row[0])

        # 2. Every meta entry must point to a valid chunk_id in DB
        if db_chunk_ids or chunks > 0:
            all_chunk_ids_in_db: set[int] = set()
            with closing(_db_connect()) as conn:
                all_chunk_ids_in_db = {r[0] for r in conn.execute("SELECT id FROM chunks").fetchall()}

            for emb_id_str, chunk_id in meta["embedding_id_to_chunk_id"].items():
                if int(chunk_id) not in all_chunk_ids_in_db:
                    orphaned_meta_entries.append(int(chunk_id))

        if orphaned_db_chunks:
            issues.append(
                f"{len(orphaned_db_chunks)} chunk(s) have embedding_id values not present in FAISS meta: "
                f"chunk ids {orphaned_db_chunks[:10]}{'...' if len(orphaned_db_chunks) > 10 else ''}. "
                "These chunks will not be found in semantic search. Fix with: wain status --fix"
            )

        if orphaned_meta_entries:
            issues.append(
                f"{len(orphaned_meta_entries)} FAISS meta entries point to chunk ids not in DB: "
                f"{orphaned_meta_entries[:10]}{'...' if len(orphaned_meta_entries) > 10 else ''}. "
                "These are stale/dangling references."
            )

    # -- Last-run timestamps ---------------------------------------------------
    _step("Loading pipeline state...")
    state = _load_state()

    def _ts(stage: str) -> str:
        entry = state.get(stage, {})
        ts = entry.get("last_run")
        return ts if ts else "never"

    # -- Summary output --------------------------------------------------------
    def tick(ok: bool) -> str:
        return "[ok]" if ok else "[FAIL]"

    def pct(n: int, total: int) -> str:
        if total == 0:
            return "  --  "
        p = n / total * 100
        return f"{p:5.1f}%"

    typer.echo("Pipeline stages:")
    typer.echo(f"  {'Stage':<12}  {'Status':<6}  {'Count':<15}  {'Complete':<8}  Last run")
    typer.echo("  " + "-" * 65)

    txt_exists = Path(config.CHAT_TXT_FILE).exists()
    typer.echo(
        f"  {'export':<12}  {tick(txt_exists):<6}  "
        f"{'file ' + ('found' if txt_exists else 'missing'):<15}  {'':8}  "
        f"{_ts('parse')}"
    )
    typer.echo(
        f"  {'parse':<12}  {tick(msgs > 0):<6}  "
        f"{msgs:>8,} msgs    {pct(msgs, msgs):<8}  {_ts('parse')}"
    )
    typer.echo(
        f"  {'chunk':<12}  {tick(chunks > 0):<6}  "
        f"{chunks:>8,} chunks  {pct(chunks, chunks):<8}  {_ts('chunk')}"
    )
    typer.echo(
        f"  {'summarize':<12}  {tick(summaries == chunks and chunks > 0):<6}  "
        f"{summaries:>4}/{chunks:<4} chunks  {pct(summaries, chunks):<8}  {_ts('summarize')}"
    )
    typer.echo(
        f"  {'embed':<12}  {tick(embedded_in_db == summaries and summaries > 0):<6}  "
        f"{embedded_in_db:>4}/{summaries:<4} chunks  {pct(embedded_in_db, summaries):<8}  {_ts('embed')}"
    )
    typer.echo(
        f"  {'faiss index':<12}  {tick(vectors > 0):<6}  "
        f"{vectors:>8,} vectors  {pct(vectors, chunks):<8}  {_ts('embed')}"
    )

    if date_range[0] != "--":
        typer.echo(f"\n  Date range: {date_range[0]} -> {date_range[1]}")

    # -- Consistency report ----------------------------------------------------
    typer.echo()
    if not issues:
        typer.echo("Consistency: [ok]  No issues found.")
    else:
        typer.echo(f"Consistency: [FAIL]  {len(issues)} issue(s) found:")
        for i, issue in enumerate(issues, 1):
            typer.echo(f"  [{i}] {issue}")

    # -- Fix -------------------------------------------------------------------
    if fix:
        typer.echo()
        if not orphaned_db_chunks:
            typer.echo("--fix: Nothing to repair.")
        else:
            typer.echo(f"--fix: Clearing stale embedding_id on {len(orphaned_db_chunks)} chunk(s)...")
            with closing(_db_connect()) as conn:
                placeholders = ",".join("?" * len(orphaned_db_chunks))
                conn.execute(
                    f"UPDATE chunks SET embedding_id = NULL WHERE id IN ({placeholders})",
                    orphaned_db_chunks,
                )
                affected = conn.execute("SELECT changes()").fetchone()[0]
                conn.commit()
            typer.echo(f"  Cleared {affected} stale embedding_id value(s). Run 'wain embed' to re-embed.")

    # -- Next step -------------------------------------------------------------
    typer.echo()
    if msgs == 0:
        if not txt_exists:
            typer.echo("Next: wain config set chat-txt-file /path/to/_chat.txt && wain parse")
        else:
            typer.echo("Next: wain parse")
    elif chunks == 0:
        typer.echo("Next: wain chunk")
    elif summaries < chunks:
        typer.echo(f"Next: wain summarize   ({chunks - summaries} chunks remaining)")
    elif embedded_in_db < summaries:
        typer.echo(f"Next: wain embed   ({summaries - embedded_in_db} chunks to embed)")
    else:
        typer.echo("Pipeline complete. Run: wain query")

    elapsed = time.monotonic() - t0
    typer.echo(f"\n  (status completed in {elapsed:.2f}s)")


# -- parse ---------------------------------------------------------------------

@app.command()
def parse(
    workspace: WorkspaceOpt = None,
    force: Annotated[bool, typer.Option("--force", help="Re-parse even if messages already exist in DB.")] = False,
    sender_self: Annotated[Optional[str], typer.Option("--sender-self", help="Your display name in the export (e.g. Alice).")] = None,
    sender_other: Annotated[Optional[str], typer.Option("--sender-other", help="The other person's display name (e.g. Bob).")] = None,
    sender_self_raw: Annotated[Optional[str], typer.Option("--sender-self-raw", help="Comma-separated raw export name(s) for sender-self.")] = None,
    chat_file: Annotated[Optional[str], typer.Option("--chat-file", help="Path to the WhatsApp _chat.txt export file.")] = None,
    export_dir: Annotated[Optional[str], typer.Option("--export-dir", help="Directory containing the export media files.")] = None,
):
    """
    Parse WhatsApp .txt export into SQLite messages.

    Reads CHAT_TXT_FILE and CHAT_EXPORT_DIR from config (or --chat-file / --export-dir).
    Sender identity can be set via --sender-self / --sender-other (saved to workspace config).
    Creates the database and tables if they don't exist.
    """
    _apply_workspace(workspace)

    # Apply CLI overrides to config resolution (highest priority)
    config.set_cli_overrides(
        sender_self=sender_self,
        sender_other=sender_other,
        sender_self_raw=sender_self_raw,
        chat_txt_file=chat_file,
        chat_export_dir=export_dir,
    )
    config.apply_cli_overrides()

    # Persist CLI args to workspace config so future runs don't need them again
    if workspace and any(v is not None for v in [sender_self, sender_other, sender_self_raw, chat_file, export_dir]):
        ws_cfg = config.get_workspace_config(workspace)
        ws_cfg.setdefault("workspace", {})
        if sender_self:     ws_cfg["workspace"]["sender_self"]    = sender_self
        if sender_other:    ws_cfg["workspace"]["sender_other"]   = sender_other
        if sender_self_raw: ws_cfg["workspace"]["sender_self_raw"] = sender_self_raw
        if chat_file:       ws_cfg["workspace"]["chat_txt_file"]  = chat_file
        if export_dir:      ws_cfg["workspace"]["chat_export_dir"] = export_dir
        config.save_workspace_config(workspace, ws_cfg)
        _step(f"Saved sender/path config to workspace '{workspace}'")

    txt = Path(config.CHAT_TXT_FILE)
    if not txt.exists():
        _abort(
            f"Chat export file not found: {config.CHAT_TXT_FILE}\n"
            "Run 'wain config set chat-txt-file /path/to/_chat.txt' or pass --chat-file."
        )
    from wain.parser import parse_export, init_db, insert_messages, insert_messages_delta

    existing = _msg_count()

    if force and existing > 0:
        _step("Force re-parse: clearing existing messages...")
        conn = init_db(config.active_db_path())
        conn.execute("DELETE FROM messages")
        conn.commit()
    else:
        conn = init_db(config.active_db_path())

    _step(f"Parsing: {config.CHAT_TXT_FILE}")
    messages = parse_export(config.CHAT_TXT_FILE)
    typer.echo(f"  Parsed {len(messages):,} messages from export")

    if force or existing == 0:
        _step("Inserting all messages (first run or --force)...")
        insert_messages(conn, messages)
        inserted, skipped = len(messages), 0
    else:
        _step("Delta insert: checking for new messages...")
        inserted, skipped = insert_messages_delta(conn, messages)

    typer.echo(f"  Inserted: {inserted:,} new  |  Skipped: {skipped:,} already present")

    conn2 = _db_connect()
    row = conn2.execute("SELECT MIN(date), MAX(date) FROM messages").fetchone()
    total = conn2.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    typer.echo(f"  Total in DB: {total:,} messages  ({row[0]} -> {row[1]})")
    conn2.close()

    _record_run("parse", message_count=total, new_messages=inserted)
    typer.echo("Done. Next: wain chunk")


# -- chunk ---------------------------------------------------------------------

@app.command()
def chunk(
    workspace: WorkspaceOpt = None,
    force: Annotated[bool, typer.Option("--force", help="Recreate chunks even if they already exist.")] = False,
):
    """
    Group messages into daily chunks.

    Each chunk = one day of conversation. Tags every message with its chunk_id.
    Safe to re-run; clears and recreates chunks each time (or use --force explicitly).
    """
    _apply_workspace(workspace)
    _require_messages()

    from wain.chunker import create_chunks, update_chunks

    conn = _db_connect()

    if force:
        _step("Force rebuild: clearing all chunks and recreating...")
        chunks_list = create_chunks(conn)
        _record_run("chunk", chunk_count=len(chunks_list))
        typer.echo(f"Rebuilt {len(chunks_list)} chunks. Next: wain summarize")
    else:
        _step("Delta chunk: processing only new/changed dates...")
        created, extended, skipped = update_chunks(conn)
        total = _chunk_count()
        typer.echo(f"  Created: {created}  |  Extended (boundary): {extended}  |  Skipped: {skipped}")
        typer.echo(f"  Total chunks in DB: {total}")
        _record_run("chunk", chunk_count=total, created=created, extended=extended)
        if created == 0 and extended == 0:
            typer.echo("No new chunks. Next: wain summarize")
        else:
            typer.echo("Done. Next: wain summarize")


# -- summarize -----------------------------------------------------------------

@app.command()
def summarize(
    workspace: WorkspaceOpt = None,
    from_id: Annotated[int, typer.Option("--from", "-f", help="Resume from chunk id (skip earlier chunks).")] = 1,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show plan without calling the API.")] = False,
    force: Annotated[bool, typer.Option("--force", help="Clear all existing summaries and re-summarize from scratch.")] = False,
):
    """
    LLM-summarize all unsummarized chunks (calls OpenAI API).

    Runs strictly sequentially -- order is required for the self-referential context window.
    Safe to interrupt and resume with --from <chunk_id>.
    """
    _apply_workspace(workspace)
    _require_chunks()
    if not dry_run:
        _require_openai()

    if force:
        _step("Force re-summarize: clearing all existing summaries and embedding IDs...")
        with closing(_db_connect()) as conn:
            conn.execute("UPDATE chunks SET summary = NULL")
            conn.execute("UPDATE chunks SET embedding_id = NULL")
            conn.commit()
        from_id = 1  # reset to beginning

    pending = _db_count(
        "SELECT COUNT(*) FROM chunks WHERE summary IS NULL AND id >= ?", (from_id,)
    )
    if pending == 0 and not dry_run:
        typer.echo("All chunks are already summarized. Nothing to do.")
        typer.echo("Run 'wain embed' next.")
        return

    import asyncio
    from wain.summarizer import run as summarizer_run

    asyncio.run(summarizer_run(start_from_chunk_id=from_id, dry_run=dry_run))

    if not dry_run:
        done_count = _summary_count()
        _record_run("summarize", summaries_complete=done_count)
        typer.echo("Next: wain embed")


# -- embed ---------------------------------------------------------------------

@app.command()
def embed(
    workspace: WorkspaceOpt = None,
    force: Annotated[bool, typer.Option("--force", help="Clear all existing embeddings and re-embed from scratch.")] = False,
):
    """
    Embed chunk summaries into the FAISS vector index (calls OpenAI API).

    Only processes chunks that have a summary but no embedding yet.
    Safe to run incrementally after each summarize batch.
    """
    _apply_workspace(workspace)
    _require_summaries()
    _require_openai()

    if force:
        _step("Force re-embed: deleting FAISS index files and clearing embedding IDs...")
        for path in (config.active_index_path(), config.active_meta_path()):
            try:
                Path(path).unlink()
                typer.echo(f"  Deleted {path}", err=True)
            except FileNotFoundError:
                pass
        with closing(_db_connect()) as conn:
            conn.execute("UPDATE chunks SET embedding_id = NULL")
            conn.commit()

    pending = _db_count("SELECT COUNT(*) FROM chunks WHERE summary IS NOT NULL AND embedding_id IS NULL")
    if pending == 0:
        typer.echo("All summarized chunks are already embedded. Nothing to do.")
        return

    _step(f"Embedding {pending} chunk summaries...")
    from wain.embedder import embed_chunk_summaries
    embed_chunk_summaries()

    embedded_count = _embedded_count()
    _record_run("embed", embedded_count=embedded_count)
    typer.echo("Next: wain query")


# -- query ---------------------------------------------------------------------

@app.command()
def query(
    workspace: WorkspaceOpt = None,
    question: Annotated[Optional[str], typer.Argument(help="Question to search for. Omit for interactive mode.")] = None,
    top_k: Annotated[int, typer.Option("--top-k", "-k", help="Number of results to return.")] = 5,
    date: Annotated[Optional[str], typer.Option("--date", "-d", help="Look up a specific date (YYYY-MM-DD).")] = None,
    fulltext: Annotated[bool, typer.Option("--fulltext", "--fts", help="Use full-text search instead of semantic.")] = False,
    hybrid: Annotated[bool, typer.Option("--hybrid/--no-hybrid", help="Combine semantic (FAISS) + keyword (FTS5) scores.")] = False,
    stats_only: Annotated[bool, typer.Option("--stats", help="Print DB stats and exit.")] = False,
    threshold: Annotated[Optional[float], typer.Option("--threshold", "-t", help=f"Minimum FAISS similarity score (default: {config.FAISS_THRESHOLD}).")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Full summary output (default: compact).")] = False,
    date_from: Annotated[Optional[str], typer.Option("--from", help="Filter results to chunks on or after this date (YYYY-MM-DD).")] = None,
    date_to: Annotated[Optional[str], typer.Option("--to", help="Filter results to chunks on or before this date (YYYY-MM-DD).")] = None,
    sender: Annotated[Optional[str], typer.Option("--sender", "-s", help="Filter to chunks containing messages from this sender (exact match, case-insensitive).")] = None,
    raw: Annotated[bool, typer.Option("--raw", help="Show raw messages instead of summary (use with --date).")] = False,
):
    """
    Query the knowledge base: semantic search, full-text search, or date lookup.

    Without arguments, launches an interactive REPL.
    With a QUESTION argument, runs a one-shot query and prints results.
    """
    _apply_workspace(workspace)
    from wain.query import (
        run_query, interactive, stats,
        search_fulltext, get_by_date, format_summary_markdown, format_compact,
        format_messages_raw, filter_by_date_range, filter_by_sender,
    )

    if stats_only:
        typer.echo(json.dumps(stats(), indent=2))
        return

    if date:
        result = get_by_date(date)
        messages = result.get("messages", [])
        if raw:
            if messages:
                typer.echo(format_messages_raw(messages))
            else:
                typer.echo(f"No messages found for {date}.")
        else:
            chunk = result.get("chunk")
            if chunk:
                typer.echo(format_summary_markdown(chunk["summary"], date=date))
            else:
                typer.echo(f"No chunk found for {date}.")
        typer.echo(f"\n{len(messages)} messages on this date.")
        return

    # Warn if index is empty -- but still allow FTS
    vectors = _faiss_vector_count()
    if vectors == 0 and not fulltext:
        typer.echo(
            "Warning: FAISS index is empty. Semantic search won't return results.\n"
            "Run 'wain embed' to build the index, or use --fulltext for keyword search.",
            err=True,
        )

    if fulltext and question:
        results = search_fulltext(question, limit=top_k * 2)
        if not results:
            typer.echo("No results.")
            return
        for r in results[:top_k]:
            typer.echo(f"[{r['date']} {r['timestamp']}] {r['sender']}: {(r['text'] or '')[:160]}")
        return

    if hybrid and question:
        from wain.search import hybrid_search
        typer.echo(f"\nHybrid search: {question!r}  "
                   f"(semantic x{config.HYBRID_SEMANTIC_WEIGHT} + keyword x{config.HYBRID_KEYWORD_WEIGHT})\n")
        results = hybrid_search(question, top_k=top_k)
        if not results:
            typer.echo("No results.")
            return
        fmt = format_summary_markdown if verbose else format_compact
        sep = "-" * 60
        for i, r in enumerate(results, 1):
            source_badge = {"semantic": "sem", "keyword": "kw ", "both": "BOTH"}[r["source"]]
            typer.echo(
                f"[{i}/{len(results)}] [{source_badge}] "
                f"combined={r['combined_score']:.3f}  "
                f"(sem={r['semantic_score']:.3f} kw={r['keyword_score']:.3f})"
            )
            typer.echo(fmt(r.get("summary", ""), date=r.get("date_start", "")))
            typer.echo(f"\n{sep}\n" if verbose else "")
        return

    if question or (date_from or date_to):
        run_query(
            question or "",
            top_k=top_k,
            threshold=threshold,
            verbose=verbose,
            date_from=date_from,
            date_to=date_to,
            sender=sender,
        )
        return

    # Interactive mode
    interactive(verbose=verbose)



# -- transcribe ----------------------------------------------------------------

@app.command()
def transcribe(
    workspace: WorkspaceOpt = None,
    backend: Annotated[Optional[str], typer.Option("--backend", help="Whisper backend: 'local' or 'api'.")] = None,
    model: Annotated[Optional[str], typer.Option("--model", help="Whisper model (local: base/small/medium; api: whisper-1).")] = None,
    language: Annotated[Optional[str], typer.Option("--language", help="Language hint (e.g. 'ru', 'en'). Empty = auto-detect.")] = None,
    limit: Annotated[Optional[int], typer.Option("--limit", "-n", help="Max number of files to transcribe (default: all).")] = None,
):
    """
    Transcribe voice notes using Whisper. Run this between parse and chunk.

    Pipeline order: parse -> transcribe -> chunk -> summarize -> embed -> query

    Transcripts are stored in messages.transcript and injected into chunk
    text by the chunker, so summarization sees voice note content.

    This step is optional -- if skipped, voice notes appear as [audio: filename]
    in summaries. Idempotent: already-transcribed messages are always skipped.
    """
    _apply_workspace(workspace)
    _require_messages()

    # Check that audio messages exist
    audio_count = _db_count("SELECT COUNT(*) FROM messages WHERE media_type = 'audio'")
    if audio_count == 0:
        typer.echo("No audio messages found in DB. Nothing to transcribe.")
        return

    # Ensure transcript column exists before counting (may be absent after --force re-parse)
    from wain.transcriber import ensure_transcript_column
    with closing(_db_connect()) as _conn:
        ensure_transcript_column(_conn)

    pending = _db_count(
        "SELECT COUNT(*) FROM messages WHERE media_type = 'audio' AND transcript IS NULL"
    )

    if pending == 0:
        typer.echo(f"All {audio_count} audio messages already transcribed. Nothing to do.")
        return

    # Resolve settings: CLI args override config
    _backend  = backend  or config.WHISPER_BACKEND
    _model    = model    or config.WHISPER_MODEL
    _language = language or config.WHISPER_LANGUAGE

    if _backend == "api":
        _require_openai()

    typer.echo(f"Transcribing {pending} audio messages  (backend={_backend}, model={_model})")
    if _language:
        typer.echo(f"  Language hint: {_language}")
    if limit:
        typer.echo(f"  Limit: {limit} files this run")
    typer.echo()

    from wain.transcriber import run_transcription
    result = run_transcription(
        backend=_backend,
        model=_model,
        language=_language,
        limit=limit,
    )

    typer.echo()
    typer.echo(f"Done.")
    typer.echo(f"  Transcribed:   {result['transcribed']}")
    typer.echo(f"  Skipped (no file): {result['skipped_no_file']}")
    typer.echo(f"  Errors:        {result['errors']}")
    typer.echo(f"  Already done:  {result['already_done']}")

    if result["transcribed"] > 0 or result["already_done"] > 0:
        typer.echo()
        typer.echo("Next: wain chunk  (transcripts will be injected into chunk text)")


# -- validate ------------------------------------------------------------------

@app.command()
def validate(
    workspace: WorkspaceOpt = None,
):
    """
    Validate all chunk summaries against the canonical schema.

    Prints a report of conforming vs non-conforming chunks.
    Exits 0 if all summaries conform, 1 if any fail.
    """
    _apply_workspace(workspace)
    from wain.schema import parse_summary_json, validate_summary, validation_issues

    _step("Reading summaries from DB...")
    try:
        with closing(_db_connect()) as conn:
            rows = conn.execute(
                "SELECT id, date_start, summary FROM chunks WHERE summary IS NOT NULL ORDER BY id"
            ).fetchall()
    except Exception as e:
        _abort(f"DB read failed: {e}")

    total = len(rows)
    if total == 0:
        typer.echo("No summaries in DB. Run 'wain summarize' first.")
        raise typer.Exit(code=0)

    _step(f"Validating {total} summaries...")

    conforming = 0
    non_conforming: list[tuple[int, str, list[str]]] = []
    unparseable: list[tuple[int, str]] = []

    for chunk_id, date, raw in rows:
        data = parse_summary_json(raw)
        if data is None:
            unparseable.append((chunk_id, date))
            continue
        issues = validation_issues(data)
        if not issues:
            conforming += 1
        else:
            non_conforming.append((chunk_id, date, issues))

    typer.echo()
    typer.echo(f"Summary validation report")
    typer.echo(f"  Total chunks:     {total}")
    typer.echo(f"  Conforming:       {conforming}  ({conforming/total*100:.1f}%)")
    typer.echo(f"  Non-conforming:   {len(non_conforming)}")
    typer.echo(f"  Unparseable:      {len(unparseable)}")

    if unparseable:
        typer.echo()
        typer.echo("Unparseable (cannot read JSON at all):")
        for chunk_id, date in unparseable:
            typer.echo(f"  chunk {chunk_id:3d}  {date}")

    if non_conforming:
        typer.echo()
        typer.echo("Non-conforming chunks:")
        for chunk_id, date, issues in non_conforming:
            typer.echo(f"  chunk {chunk_id:3d}  {date}  -- {'; '.join(issues)}")
        typer.echo()
        typer.echo("Run 'python scripts/migrate_summaries.py' to fix field-name issues.")
        raise typer.Exit(code=1)

    if unparseable:
        raise typer.Exit(code=1)

    typer.echo()
    typer.echo("All summaries conform to canonical schema. [ok]")


# -- describe ------------------------------------------------------------------

@app.command()
def describe(
    workspace: WorkspaceOpt = None,
    limit: Annotated[Optional[int], typer.Option("--limit", "-n", help="Max images to process in this run.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Clear existing descriptions and re-describe.")] = False,
):
    """
    Describe images using the OpenAI vision API (calls OpenAI API).

    Reads image messages from the DB, encodes each file as base64, and stores
    a brief description in messages.description for use by the summarizer.

    Set VISION_BACKEND=none to skip without error.
    Pipeline order: parse -> transcribe -> describe -> chunk -> summarize -> embed -> query
    """
    _apply_workspace(workspace)
    _require_messages()

    if config.VISION_BACKEND == "none":
        typer.echo("VISION_BACKEND=none -- skipping image description.")
        return

    _require_openai()

    from wain.describer import ensure_description_column, run_description

    # Ensure column exists before counting
    with closing(_db_connect()) as _conn:
        ensure_description_column(_conn)

    image_count = _db_count("SELECT COUNT(*) FROM messages WHERE media_type = 'image'")
    if image_count == 0:
        typer.echo("No image messages found in DB. Nothing to describe.")
        return

    if force:
        _step("Force re-describe: clearing existing descriptions...")
        with closing(_db_connect()) as conn:
            conn.execute("UPDATE messages SET description = NULL WHERE media_type = 'image'")
            conn.commit()

    pending = _db_count(
        "SELECT COUNT(*) FROM messages WHERE media_type = 'image' AND description IS NULL AND media_path IS NOT NULL"
    )
    if pending == 0:
        typer.echo(f"All {image_count} images already described. Nothing to do.")
        return

    _step(f"Describing {pending} images (model: {config.VISION_MODEL})...")
    result = run_description(limit=limit, force=False)  # force already applied above

    typer.echo(f"  Described: {result['described']}  |  "
               f"Skipped (no file): {result['skipped_no_file']}  |  "
               f"Errors: {result['errors']}")
    _record_run("describe", described=result["described"])
    typer.echo("Next: wain chunk")



# -- run -----------------------------------------------------------------------

@app.command()
def run(
    workspace: WorkspaceOpt = None,
    skip_transcribe: Annotated[bool, typer.Option("--skip-transcribe", help="Skip the transcribe stage (voice notes).")] = False,
    skip_describe:   Annotated[bool, typer.Option("--skip-describe",   help="Skip the describe stage (images).")] = False,
):
    """
    Run the full pipeline in one shot:
    parse -> transcribe -> describe -> chunk -> summarize -> embed

    Each stage is delta-aware -- safe to re-run on an existing workspace.
    Use --skip-transcribe or --skip-describe to omit optional media stages.
    """
    typer.echo("=== wain run: full pipeline ===\n")

    typer.echo("[ 1/6 ] parse")
    parse(workspace=workspace)
    typer.echo()

    if not skip_transcribe:
        typer.echo("[ 2/6 ] transcribe")
        transcribe(workspace=workspace)
        typer.echo()
    else:
        typer.echo("[ 2/6 ] transcribe  (skipped)")

    if not skip_describe:
        typer.echo("[ 3/6 ] describe")
        describe(workspace=workspace)
        typer.echo()
    else:
        typer.echo("[ 3/6 ] describe    (skipped)")

    typer.echo("[ 4/6 ] chunk")
    chunk(workspace=workspace)
    typer.echo()

    typer.echo("[ 5/6 ] summarize")
    summarize(workspace=workspace)
    typer.echo()

    typer.echo("[ 6/6 ] embed")
    embed(workspace=workspace)
    typer.echo()

    typer.echo("=== Pipeline complete. Run: wain query ===")

# -- init ----------------------------------------------------------------------

@app.command()
def init(
    name: Annotated[str, typer.Argument(help="Workspace name (e.g. 'alice', 'family').")],
    force: Annotated[bool, typer.Option("--force", help="Overwrite if workspace already exists.")] = False,
):
    """
    Create a new named workspace at ~/.wain/workspaces/<name>/.

    Each workspace is fully isolated: its own chat.db, FAISS index, and config.
    Run the full pipeline once per workspace:

      wain parse     --workspace <name>
      wain transcribe --workspace <name>
      wain chunk     --workspace <name>
      wain summarize --workspace <name>
      wain embed     --workspace <name>
      wain query     --workspace <name>
    """
    from pathlib import Path as _Path
    ws = config.get_workspace_paths(name)
    ws_root = _Path(ws.root)

    if ws_root.exists() and not force:
        _abort(
            f"Workspace '{name}' already exists at {ws.root}. "
            "Use --force to reinitialise."
        )

    ws_root.mkdir(parents=True, exist_ok=True)

    # Write a marker file with workspace metadata
    marker = ws_root / "workspace.ini"
    import time as _time
    marker.write_text(
        "[workspace]\nname = " + name + "\ncreated = " + _time.strftime("%Y-%m-%dT%H:%M:%S") + "\n"
    )

    typer.echo(f"Workspace '{name}' created at {ws.root}")
    typer.echo(f"  DB:    {ws.db_path}")
    typer.echo(f"  Index: {ws.index_path}")
    typer.echo()
    typer.echo(f"Next: wain config set chat-export-dir /path/to/export --workspace {name}")
    typer.echo(f"Then: wain parse --workspace {name}")


# -- list ----------------------------------------------------------------------

@app.command(name="list")
def list_workspaces():
    """
    List all workspaces under ~/.wain/workspaces/ with basic pipeline stats.
    """
    workspaces = config.list_workspaces()

    if not workspaces:
        typer.echo(f"No workspaces found under {config.WORKSPACE_ROOT}")
        typer.echo("Create one with: wain init <name>")
        return

    typer.echo("Workspaces in " + config.WORKSPACE_ROOT + ":")
    typer.echo()
    typer.echo(f"  {'Name':<20} {'Messages':>10} {'Chunks':>8} {'Summarized':>12} {'Embedded':>10}")
    typer.echo("  " + "-" * 64)

    for ws in workspaces:
        try:
            with closing(sqlite3.connect(ws.db_path)) as conn:
                msgs    = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                chunks  = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                summ    = conn.execute("SELECT COUNT(*) FROM chunks WHERE summary IS NOT NULL").fetchone()[0]
                emb     = conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding_id IS NOT NULL").fetchone()[0]
            typer.echo(f"  {ws.name:<20} {msgs:>10,} {chunks:>8} {summ:>10}/{chunks:<1} {emb:>8}/{chunks}")
        except Exception:
            typer.echo(f"  {ws.name:<20} {'(empty or no DB)'}")


# -- entry point ---------------------------------------------------------------

if __name__ == "__main__":
    app()

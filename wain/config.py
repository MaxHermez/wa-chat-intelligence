"""
config.py -- central configuration for wa-chat-intelligence.

Config resolution order (highest to lowest priority):
  1. CLI arguments (set via set_cli_overrides() at command entry)
  2. Workspace config file: ~/.wain/workspaces/<name>/config.toml
  3. Global config file:    ~/.wain/config.toml
  4. Environment variables  (backward-compat fallback)

To manage config files, use:
  wain config set <key> <value> [--workspace <name>]
  wain config show [--workspace <name>]

Environment variables still work as the lowest-priority fallback.
"""

import os
import tomllib
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# TOML config loading + resolution
# ---------------------------------------------------------------------------

# Root dir for global config and workspaces
_WAIN_HOME: Path = Path.home() / ".wain"
_GLOBAL_CONFIG_PATH: Path = _WAIN_HOME / "config.toml"

# In-memory config layers — populated by load_global_config() / set_workspace()
_global_toml:    dict = {}
_workspace_toml: dict = {}
_cli_overrides:  dict = {}


def _load_toml(path: Path) -> dict:
    """Load a TOML file; return {} if absent or unreadable."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return {}


def load_global_config() -> None:
    """Load ~/.wain/config.toml into the global layer. Called once at startup."""
    global _global_toml
    _global_toml = _load_toml(_GLOBAL_CONFIG_PATH)


def _load_workspace_toml(name: str) -> None:
    """Load the per-workspace config.toml into the workspace layer."""
    global _workspace_toml
    path = _WAIN_HOME / "workspaces" / name / "config.toml"
    _workspace_toml = _load_toml(path)


def set_cli_overrides(**kwargs: str | None) -> None:
    """Store CLI-level overrides (highest priority). None values are ignored."""
    global _cli_overrides
    _cli_overrides.update({k: v for k, v in kwargs.items() if v is not None})


def clear_cli_overrides() -> None:
    """Reset CLI overrides (useful in tests)."""
    global _cli_overrides
    _cli_overrides = {}


def _resolve(key: str, env_default: str) -> str:
    """
    Resolve one config value using the priority chain:
      CLI override > workspace TOML [workspace] > global TOML [defaults] > env_default
    """
    val, _ = _resolve_with_source(key, env_default)
    return val


def _resolve_with_source(key: str, env_default: str) -> tuple[str, str]:
    """Like _resolve() but also returns the source layer name."""
    if key in _cli_overrides:
        return _cli_overrides[key], "cli"
    ws_section = _workspace_toml.get("workspace", {})
    if key in ws_section:
        return str(ws_section[key]), "workspace toml"
    g_section = _global_toml.get("defaults", {})
    if key in g_section:
        return str(g_section[key]), "global toml"
    # Check if the value differs from the hardcoded default (meaning env var is set)
    return env_default, "env" if os.environ.get(_ENV_VAR_MAP.get(key, ""), None) is not None else "default"


# Map internal config keys to their env var names for source attribution
_ENV_VAR_MAP = {
    "sender_self":        "SENDER_SELF",
    "sender_other":       "SENDER_OTHER",
    "sender_self_raw":    "SENDER_SELF_RAW",
    "chat_export_dir":    "CHAT_EXPORT_DIR",
    "chat_txt_file":      "CHAT_TXT_FILE",
    "openai_api_key":     "OPENAI_API_KEY",
    "summarizer_context": "SUMMARIZER_CONTEXT",
}


def _resolve_api_key() -> str:
    """Resolve OPENAI_API_KEY: CLI override > keyring > global TOML > env var."""
    val, _ = _resolve_api_key_with_source()
    return val


def _resolve_api_key_with_source() -> tuple[str, str]:
    """Like _resolve_api_key() but also returns the source layer name."""
    if "openai_api_key" in _cli_overrides:
        return _cli_overrides["openai_api_key"], "cli"
    try:
        import keyring as _kr
        stored = _kr.get_password("wain", "openai_api_key")
        if stored:
            return stored, "keyring"
    except Exception:
        pass
    toml_key = _global_toml.get("openai", {}).get("api_key", "")
    if toml_key:
        return toml_key, "global toml"
    env_val = os.environ.get("OPENAI_API_KEY", "")
    if env_val:
        return env_val, "env"
    return "", "default"


def resolve_sources() -> dict[str, tuple[str, str]]:
    """Return {key: (value, source)} for all resolvable config values.

    Used by `wain config show` to display source attribution.
    """
    sources = {}
    for key, env_var in _ENV_VAR_MAP.items():
        if key == "openai_api_key":
            val, src = _resolve_api_key_with_source()
            # Mask API key for display
            if val and len(val) > 8:
                val = val[:4] + "..." + val[-4:]
            sources[key] = (val or "(not set)", src)
        else:
            env_default = os.environ.get(env_var, "")
            sources[key] = _resolve_with_source(key, env_default)
    return sources


def write_toml(path: Path, data: dict) -> None:
    """Write a TOML file, creating parent directories as needed."""
    import tomli_w
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)


def save_global_config(data: dict) -> None:
    write_toml(_GLOBAL_CONFIG_PATH, data)


def save_workspace_config(name: str, data: dict) -> None:
    write_toml(_WAIN_HOME / "workspaces" / name / "config.toml", data)


def get_global_config() -> dict:
    """Return current in-memory global TOML (merged with on-disk if needed)."""
    return _load_toml(_GLOBAL_CONFIG_PATH)


def get_workspace_config(name: str) -> dict:
    """Return current on-disk workspace TOML for `name`."""
    return _load_toml(_WAIN_HOME / "workspaces" / name / "config.toml")


# Load global config immediately at import time so constants below can use it.
# _refresh_sender_vars() is called after module-level vars are defined (see bottom of file).

# Project root -- two levels up from this file (project_root/wain/config.py).
_ROOT = Path(__file__).parent.parent

# Data directory -- where runtime files (DB, FAISS index, state) live.
_DATA = _ROOT / "data"


def _env_path(var: str, default: Path) -> str:
    """Return env var value (as str) or str(default) if unset."""
    return os.environ.get(var, str(default))


# -- Database -----------------------------------------------------------------
DB_PATH: str = _env_path("DB_PATH", _DATA / "chat.db")

# -- FAISS index ---------------------------------------------------------------
INDEX_PATH: str = _env_path("INDEX_PATH", _DATA / "chat.faiss")
META_PATH: str  = _env_path("META_PATH",  _DATA / "chat_faiss_meta.json")

# -- Pipeline state ------------------------------------------------------------
STATE_PATH: str = _env_path("STATE_PATH", _DATA / "pipeline_state.json")

# -- WhatsApp export -----------------------------------------------------------
CHAT_EXPORT_DIR: str = _env_path("CHAT_EXPORT_DIR", _ROOT / "export")
_default_txt = Path(CHAT_EXPORT_DIR) / "_chat.txt"
CHAT_TXT_FILE: str = _env_path("CHAT_TXT_FILE", _default_txt)

# -- OpenAI --------------------------------------------------------------------
# Populated by _refresh_sender_vars() via _resolve_api_key() at module init.
OPENAI_API_KEY: str = ""

# -- Sender identity -----------------------------------------------------------
SENDER_SELF:  str = os.environ.get("SENDER_SELF",  "Me")
SENDER_OTHER: str = os.environ.get("SENDER_OTHER", "Them")

_self_raw_env = os.environ.get("SENDER_SELF_RAW", SENDER_SELF)
SENDER_SELF_RAW: list[str] = [s.strip().lower() for s in _self_raw_env.split(",") if s.strip()]

SUMMARIZER_CONTEXT: str = os.environ.get(
    "SUMMARIZER_CONTEXT",
    f"You are analyzing a WhatsApp conversation between {SENDER_SELF} and {SENDER_OTHER}."
)

# -- Whisper transcription -----------------------------------------------------
# Backend: "local" uses the whisper CLI (openai-whisper must be installed);
#          "api"   uses the OpenAI Audio Transcriptions API (needs OPENAI_API_KEY).
WHISPER_BACKEND: str = os.environ.get("WHISPER_BACKEND", "local")

# Model name -- for local: "tiny", "base", "small", "medium", "large"
#              for api:   "whisper-1"
WHISPER_MODEL: str = os.environ.get("WHISPER_MODEL", "base")

# Optional: language hint passed to Whisper (e.g. "ru", "en"). Empty = auto-detect.
WHISPER_LANGUAGE: str = os.environ.get("WHISPER_LANGUAGE", "")

# -- Multi-workspace support ---------------------------------------------------
import dataclasses

# Root directory for all workspaces.
WORKSPACE_ROOT: str = os.environ.get(
    "WAINTEL_WORKSPACE_ROOT",
    str(Path.home() / ".wain" / "workspaces")
)

# Default workspace name from env (can be overridden per-command with --workspace).
WAINTEL_WORKSPACE: str = os.environ.get("WAINTEL_WORKSPACE", "")


@dataclasses.dataclass
class WorkspacePaths:
    """Resolved file paths for one workspace."""
    name: str
    root: str       # ~/.wain/workspaces/<name>/
    db_path: str
    index_path: str
    meta_path: str
    state_path: str


def get_workspace_paths(name: str) -> WorkspacePaths:
    """Return resolved file paths for a named workspace."""
    root = str(Path(WORKSPACE_ROOT) / name)
    return WorkspacePaths(
        name=name,
        root=root,
        db_path=str(Path(root) / "chat.db"),
        index_path=str(Path(root) / "chat.faiss"),
        meta_path=str(Path(root) / "chat_faiss_meta.json"),
        state_path=str(Path(root) / "pipeline_state.json"),
    )


# -- Active workspace context --------------------------------------------------
# Set by the CLI at command invocation time. None = use global config defaults.

class _ActiveWorkspace:
    db_path:    str | None = None
    index_path: str | None = None
    meta_path:  str | None = None
    state_path: str | None = None


_aws = _ActiveWorkspace()


def set_workspace(name: str | None) -> WorkspacePaths | None:
    """
    Activate a named workspace for this process.
    Call this at CLI command entry before any pipeline operations.
    Loads the workspace config.toml and refreshes module-level settings.
    Pass None to reset to global config defaults.
    """
    global SENDER_SELF, SENDER_OTHER, SENDER_SELF_RAW, SUMMARIZER_CONTEXT
    global CHAT_EXPORT_DIR, CHAT_TXT_FILE

    if not name:
        _aws.db_path = _aws.index_path = _aws.meta_path = _aws.state_path = None
        global _workspace_toml
        _workspace_toml = {}
        return None

    _load_workspace_toml(name)
    ws = get_workspace_paths(name)
    _aws.db_path    = ws.db_path
    _aws.index_path = ws.index_path
    _aws.meta_path  = ws.meta_path
    _aws.state_path = ws.state_path

    # Refresh sender identity from TOML (CLI overrides applied after this in apply_cli_overrides)
    _refresh_sender_vars()

    return ws


def apply_cli_overrides() -> None:
    """
    Apply any CLI overrides that were set via set_cli_overrides().
    Call this after set_workspace() when CLI args are present.
    """
    global SENDER_SELF, SENDER_OTHER, SENDER_SELF_RAW, SUMMARIZER_CONTEXT
    global CHAT_EXPORT_DIR, CHAT_TXT_FILE
    _refresh_sender_vars()


def _refresh_sender_vars() -> None:
    """Re-resolve and update mutable module-level sender/path vars from current config layers."""
    global SENDER_SELF, SENDER_OTHER, SENDER_SELF_RAW, SUMMARIZER_CONTEXT
    global CHAT_EXPORT_DIR, CHAT_TXT_FILE, OPENAI_API_KEY

    SENDER_SELF  = _resolve("sender_self",  os.environ.get("SENDER_SELF",  "Me"))
    SENDER_OTHER = _resolve("sender_other", os.environ.get("SENDER_OTHER", "Them"))

    raw_default = os.environ.get("SENDER_SELF_RAW", SENDER_SELF)
    raw_str = _resolve("sender_self_raw", raw_default)
    SENDER_SELF_RAW = [s.strip().lower() for s in raw_str.split(",") if s.strip()]

    SUMMARIZER_CONTEXT = _resolve(
        "summarizer_context",
        os.environ.get("SUMMARIZER_CONTEXT",
                       f"You are analyzing a WhatsApp conversation between {SENDER_SELF} and {SENDER_OTHER}.")
    )

    CHAT_EXPORT_DIR = _resolve("chat_export_dir", os.environ.get("CHAT_EXPORT_DIR", CHAT_EXPORT_DIR))
    CHAT_TXT_FILE   = _resolve("chat_txt_file",   os.environ.get("CHAT_TXT_FILE",   CHAT_TXT_FILE))

    OPENAI_API_KEY = _resolve_api_key()


def active_db_path()    -> str: return _aws.db_path    or DB_PATH
def active_index_path() -> str: return _aws.index_path or INDEX_PATH
def active_meta_path()  -> str: return _aws.meta_path  or META_PATH
def active_state_path() -> str: return _aws.state_path or STATE_PATH


def list_workspaces() -> list[WorkspacePaths]:
    """Return WorkspacePaths for every directory under WORKSPACE_ROOT."""
    root = Path(WORKSPACE_ROOT)
    if not root.exists():
        return []
    return sorted(
        [get_workspace_paths(d.name) for d in root.iterdir() if d.is_dir()],
        key=lambda w: w.name,
    )

# -- FAISS similarity threshold -----------------------------------------------
# Cosine similarity gate for semantic search. Empirical max for this corpus
# is ~0.45 (text-embedding-3-small); 0.30 passes useful results through.
FAISS_THRESHOLD: float = float(os.environ.get("FAISS_THRESHOLD", "0.20"))
# Scores below this value are shown with a "(low confidence)" label in query output.
# Keeps the retrieval net wide (FAISS_THRESHOLD) while signalling marginal matches.
FAISS_LOW_CONFIDENCE_THRESHOLD: float = float(os.environ.get("FAISS_LOW_CONFIDENCE_THRESHOLD", "0.30"))

# -- Hybrid search weights -----------------------------------------------------
# Combined score = w_semantic * cosine_score + w_keyword * normalized_bm25_score
HYBRID_SEMANTIC_WEIGHT: float = float(os.environ.get("WAINTEL_HYBRID_SEMANTIC_WEIGHT", "0.7"))
HYBRID_KEYWORD_WEIGHT:  float = float(os.environ.get("WAINTEL_HYBRID_KEYWORD_WEIGHT",  "0.3"))

# -- Model selection -----------------------------------------------------------
# Changing WAINTEL_EMBEDDING_MODEL requires re-embedding all chunks since
# vector dimensions differ between models (run: wain embed --force).
SUMMARIZER_MODEL:  str = os.environ.get("WAINTEL_SUMMARIZER_MODEL",  "gpt-5-mini")
EMBEDDING_MODEL:   str = os.environ.get("WAINTEL_EMBEDDING_MODEL",   "text-embedding-3-small")

# -- Vision (image description) ------------------------------------------------
# Set VISION_BACKEND=none to skip all image description without error.
VISION_BACKEND: str = os.environ.get("VISION_BACKEND", "api")
VISION_MODEL:   str = os.environ.get("VISION_MODEL",   "gpt-5.2")
# -- Date parsing --------------------------------------------------------------
# Set to false for US exports where dates are MM/DD/YYYY.
# Default: true (DD/MM/YYYY -- most regions outside the US)
DATE_DAYFIRST: bool = os.environ.get("WAINTEL_DATE_DAYFIRST", "true").lower() in ("true", "1", "yes")

# -- Bootstrap: apply global TOML on top of env-var defaults -----------------
# Called after all module-level constants are defined so _refresh_sender_vars()
# has valid fallback values. Workspace TOML is applied later by set_workspace().
load_global_config()
_refresh_sender_vars()

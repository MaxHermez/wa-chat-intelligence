"""
schema.py -- canonical ChunkSummary schema and validation utilities.

The canonical field names are the source of truth for the entire pipeline.
All field-name variants produced by older summarizer runs are mapped here.
"""

from __future__ import annotations

import json
import re
from typing import TypedDict


# -- Canonical schema ----------------------------------------------------------

class ChunkSummary(TypedDict):
    date: str                 # YYYY-MM-DD
    message_count: int
    mood: str                 # canonical (was: emotional_tone)
    energy_level: str
    initiator: str
    topics: list[str]         # canonical (was: main_topics)
    key_moments: list[str]
    plans: list[str]
    cancellations: list[str]
    media_context: str
    relationship_signal: str
    needs_prior_context: bool
    summary: str              # canonical (was: overview, narrative)


REQUIRED_FIELDS: frozenset[str] = frozenset(ChunkSummary.__annotations__.keys())

# Map variant field names -> canonical names.
# Any field key not in this map and not already canonical is left as-is
# (extra fields are permitted but not required).
FIELD_ALIASES: dict[str, str] = {
    "emotional_tone": "mood",
    "main_topics":    "topics",
    "overview":       "summary",
    "narrative":      "summary",
}

# JSON schema block injected into the summarizer system prompt.
# Keeps the LLM output aligned with the canonical schema.
JSON_SCHEMA_BLOCK = """\
Output a JSON object with EXACTLY these fields (no extras, no omissions):
{
  "date": "YYYY-MM-DD",
  "message_count": <integer>,
  "mood": "<warm|tense|distant|playful|heavy|mixed|warm/mixed|...>",
  "energy_level": "<high|medium|low>",
  "initiator": "<SENDER_SELF|SENDER_OTHER|equal>",
  "topics": ["<string>", ...],
  "key_moments": ["<string>", ...],
  "plans": ["<string>", ...],
  "cancellations": ["<string>", ...],
  "media_context": "<string>",
  "relationship_signal": "<string>",
  "needs_prior_context": <true|false>,
  "summary": "<2-4 paragraph narrative>"
}
Respond with ONLY the JSON object -- no markdown fences, no preamble, no trailing text."""


# -- Parsing -------------------------------------------------------------------

def parse_summary_json(raw: str) -> dict | None:
    """
    Parse a raw summary string into a dict.

    Handles:
    - Markdown code fences (```json ... ```)
    - Embedded control characters from message text (uses strict=False)
    - Returns None if the string cannot be parsed at all.
    """
    if not raw:
        return None
    # Strip markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    # Try strict first, fall back to lenient
    for strict in (True, False):
        try:
            return json.loads(cleaned, strict=strict)
        except json.JSONDecodeError:
            continue
    return None


# -- Normalisation -------------------------------------------------------------

def normalize_summary(data: dict) -> dict:
    """
    Apply field aliases and return a new dict with canonical key names.

    Does NOT add missing fields or remove extra fields -- call
    validate_summary() after to check completeness.
    """
    result: dict = {}
    for key, value in data.items():
        canonical = FIELD_ALIASES.get(key, key)
        # Don't overwrite if the canonical key was already present
        if canonical not in result:
            result[canonical] = value
        # If canonical key already exists and this is a duplicate alias, skip
    return result


# -- Validation ----------------------------------------------------------------

def validate_summary(data: dict) -> bool:
    """Return True if data contains all required canonical fields."""
    return REQUIRED_FIELDS.issubset(data.keys())


def validation_issues(data: dict) -> list[str]:
    """
    Return a list of human-readable issue strings.
    Empty list means the summary is fully conforming.
    """
    issues: list[str] = []
    missing = REQUIRED_FIELDS - data.keys()
    if missing:
        issues.append(f"Missing fields: {', '.join(sorted(missing))}")
    # Variant names still present (weren't normalized)
    stale = set(FIELD_ALIASES.keys()) & data.keys()
    if stale:
        issues.append(f"Non-canonical field names (run migrate): {', '.join(sorted(stale))}")
    return issues


def serialize_summary(data: dict) -> str:
    """Serialize a summary dict to clean JSON (no control chars, no fences)."""
    return json.dumps(data, ensure_ascii=False, indent=2)

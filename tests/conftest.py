"""Shared test fixtures for wain tests."""

import sqlite3
import tempfile
import os
from pathlib import Path

import pytest

from wain.parser import init_db


@pytest.fixture
def tmp_db(tmp_path):
    """Fresh SQLite DB with schema applied. Returns (connection, path)."""
    db_path = str(tmp_path / "test.db")
    conn = init_db(db_path)
    yield conn, db_path
    conn.close()


@pytest.fixture
def sample_export(tmp_path):
    """Temp directory with a fake _chat.txt. Returns path to the txt file."""
    chat_txt = tmp_path / "_chat.txt"
    chat_txt.write_text(
        "1/10/2025, 9:00 am - Alice: Good morning!\n"
        "1/10/2025, 9:01 am - Bob: Morning! How are you?\n"
        "1/10/2025, 9:02 am - Alice: Pretty good, thinking about\n"
        " the trip to Portugal\n"
        "1/10/2025, 9:05 am - Bob: IMG-20251001-WA0001.jpg (file attached)\n"
        "1/10/2025, 9:10 am - Alice: PTT-20251001-WA0001.opus (file attached)\n"
        "1/10/2025, 10:00 am - Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them. Tap to learn more.\n"
        "2/10/2025, 8:00 am - Alice: New day!\n"
        "2/10/2025, 8:30 am - Bob: Let's plan the trip\n",
        encoding="utf-8",
    )
    return str(chat_txt)


@pytest.fixture
def populated_db(tmp_db, sample_export):
    """DB with parsed sample messages inserted. Returns (connection, path)."""
    conn, db_path = tmp_db
    from wain import parser
    import wain.config as cfg

    # Temporarily override config for parsing
    old_dayfirst = cfg.DATE_DAYFIRST
    old_self_raw = cfg.SENDER_SELF_RAW
    old_self = cfg.SENDER_SELF
    old_other = cfg.SENDER_OTHER
    old_parser_self = parser.SENDER_SELF
    old_parser_other = parser.SENDER_OTHER

    cfg.DATE_DAYFIRST = True
    cfg.SENDER_SELF_RAW = ["alice"]
    cfg.SENDER_SELF = "Alice"
    cfg.SENDER_OTHER = "Bob"
    parser.SENDER_SELF = "Alice"
    parser.SENDER_OTHER = "Bob"

    messages = parser.parse_export(sample_export)
    parser.insert_messages(conn, messages)

    yield conn, db_path

    cfg.DATE_DAYFIRST = old_dayfirst
    cfg.SENDER_SELF_RAW = old_self_raw
    cfg.SENDER_SELF = old_self
    cfg.SENDER_OTHER = old_other
    parser.SENDER_SELF = old_parser_self
    parser.SENDER_OTHER = old_parser_other


@pytest.fixture
def clean_config():
    """Reset config overrides between tests."""
    from wain.config import clear_cli_overrides
    clear_cli_overrides()
    yield
    clear_cli_overrides()

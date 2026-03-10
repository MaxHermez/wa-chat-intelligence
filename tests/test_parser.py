"""Tests for wain.parser -- WhatsApp export parsing."""

import pytest
import wain.config as cfg
from wain import parser as parser_mod
from wain.parser import (
    parse_export,
    normalize_sender,
    insert_messages_delta,
    _media_type,
    LINE_RE,
    SYSTEM_RE,
    MEDIA_RE,
)


@pytest.fixture(autouse=True)
def _patch_sender_config():
    """Patch both config and parser module sender references for all tests."""
    old = {
        "cfg_dayfirst": cfg.DATE_DAYFIRST,
        "cfg_raw": cfg.SENDER_SELF_RAW,
        "cfg_self": cfg.SENDER_SELF,
        "cfg_other": cfg.SENDER_OTHER,
        "parser_self": parser_mod.SENDER_SELF,
        "parser_other": parser_mod.SENDER_OTHER,
    }
    cfg.DATE_DAYFIRST = True
    cfg.SENDER_SELF_RAW = ["alice"]
    cfg.SENDER_SELF = "Alice"
    cfg.SENDER_OTHER = "Bob"
    parser_mod.SENDER_SELF = "Alice"
    parser_mod.SENDER_OTHER = "Bob"
    yield
    cfg.DATE_DAYFIRST = old["cfg_dayfirst"]
    cfg.SENDER_SELF_RAW = old["cfg_raw"]
    cfg.SENDER_SELF = old["cfg_self"]
    cfg.SENDER_OTHER = old["cfg_other"]
    parser_mod.SENDER_SELF = old["parser_self"]
    parser_mod.SENDER_OTHER = old["parser_other"]


class TestLineRegex:
    def test_standard_message(self):
        m = LINE_RE.match("1/10/2025, 9:00 am - Alice: Hello world")
        assert m is not None
        assert m.group(3).strip() == "Alice"
        assert m.group(4) == "Hello world"

    def test_24h_format(self):
        m = LINE_RE.match("01/10/2025, 14:30 - Bob: Hey there")
        assert m is not None
        assert m.group(3).strip() == "Bob"

    def test_media_message(self):
        m = LINE_RE.match("1/10/2025, 9:05 am - Bob: IMG-20251001-WA0001.jpg (file attached)")
        assert m is not None
        body = m.group(4)
        assert MEDIA_RE.match(body.strip()) is not None

    def test_system_message(self):
        line = "1/10/2025, 10:00 am - Messages and calls are end-to-end encrypted."
        assert LINE_RE.match(line) is None
        assert SYSTEM_RE.match(line) is not None


class TestMediaType:
    def test_image(self):
        assert _media_type("photo.jpg") == "image"
        assert _media_type("pic.png") == "image"
        assert _media_type("img.webp") == "image"

    def test_audio(self):
        assert _media_type("voice.opus") == "audio"
        assert _media_type("note.ogg") == "audio"
        assert _media_type("song.mp3") == "audio"

    def test_video(self):
        assert _media_type("clip.mp4") == "video"

    def test_document(self):
        assert _media_type("file.pdf") == "document"

    def test_other(self):
        assert _media_type("data.xyz") == "other"

    def test_none(self):
        assert _media_type("") is None
        assert _media_type(None) is None


class TestNormalizeSender:
    def test_self_match(self):
        assert normalize_sender("Alice") == "Alice"
        assert normalize_sender("alice") == "Alice"

    def test_self_raw_variants(self):
        cfg.SENDER_SELF_RAW = ["alice", "al"]
        assert normalize_sender("Al") == "Alice"

    def test_other(self):
        assert normalize_sender("Bob") == "Bob"
        assert normalize_sender("Unknown") == "Bob"


class TestParseExport:
    def test_parses_messages(self, sample_export):
        msgs = parse_export(sample_export)
        # 7 user messages (system/encrypted notice excluded), spanning 2 days
        assert len(msgs) == 7
        assert msgs[0]["sender"] == "Alice"
        assert msgs[0]["text"] == "Good morning!"
        assert msgs[0]["date"] == "2025-10-01"

    def test_multiline_message(self, sample_export):
        msgs = parse_export(sample_export)
        # Third message has a continuation line
        assert "the trip to Portugal" in msgs[2]["text"]
        assert "\n" in msgs[2]["text"]

    def test_media_detection(self, sample_export):
        msgs = parse_export(sample_export)
        img = msgs[3]
        assert img["media_file"] == "IMG-20251001-WA0001.jpg"
        assert img["media_type"] == "image"
        assert img["text"] is None

        audio = msgs[4]
        assert audio["media_file"] == "PTT-20251001-WA0001.opus"
        assert audio["media_type"] == "audio"

    def test_second_day_messages(self, sample_export):
        msgs = parse_export(sample_export)
        day2 = [m for m in msgs if m["date"] == "2025-10-02"]
        assert len(day2) == 2


class TestDeltaInsert:
    def test_first_insert(self, tmp_db, sample_export):
        conn, _ = tmp_db
        msgs = parse_export(sample_export)
        inserted, skipped = insert_messages_delta(conn, msgs)
        assert inserted == 7
        assert skipped == 0

    def test_idempotent_reinsert(self, tmp_db, sample_export):
        conn, _ = tmp_db
        msgs = parse_export(sample_export)
        insert_messages_delta(conn, msgs)
        inserted, skipped = insert_messages_delta(conn, msgs)
        assert inserted == 0
        assert skipped == 7

"""Tests for wain.query -- filtering and formatting."""

import json
from wain.query import (
    filter_by_date_range, format_compact, format_summary_markdown,
    format_messages_raw, parse_summary,
)


SAMPLE_SUMMARY = json.dumps({
    "date": "2025-10-01",
    "message_count": 42,
    "energy_level": "high",
    "mood": "warm",
    "initiator": "Alice",
    "topics": ["travel", "cooking"],
    "key_moments": ["Alice proposed the trip"],
    "plans": ["Portugal in March"],
    "cancellations": [],
    "media_context": "2 voice notes",
    "relationship_signal": "engaged",
    "needs_prior_context": False,
    "summary": "Alice and Bob discussed travel plans and shared recipes.",
})


class TestParseSummary:
    def test_valid_json(self):
        s = parse_summary(SAMPLE_SUMMARY)
        assert s["mood"] == "warm"
        assert s["message_count"] == 42

    def test_invalid_json(self):
        assert parse_summary("not json") is None

    def test_none_input(self):
        assert parse_summary(None) is None

    def test_empty_string(self):
        assert parse_summary("") is None


class TestFormatCompact:
    def test_unparseable_falls_back_to_raw(self):
        result = format_compact("raw text fallback", date="2025-10-01")
        assert "2025-10-01" in result
        assert "raw text fallback" in result

    def test_low_confidence_flagged(self):
        result = format_compact(SAMPLE_SUMMARY, date="2025-10-01", score=0.25)
        assert "low confidence" in result


class TestFormatSummaryMarkdown:
    def test_unparseable_falls_back(self):
        result = format_summary_markdown(None)
        assert result == "(no summary)"

        result = format_summary_markdown("just raw text")
        assert result == "just raw text"


class TestFormatMessagesRaw:
    def test_text_message(self):
        msgs = [{"timestamp": "2025-10-01T09:00:00", "sender": "Alice",
                 "text": "Good morning!", "media_type": None, "media_file": None,
                 "transcript": None, "description": None}]
        result = format_messages_raw(msgs)
        assert "[2025-10-01T09:00:00] Alice:" in result
        assert "Good morning!" in result

    def test_audio_with_transcript(self):
        msgs = [{"timestamp": "2025-10-01T10:00:00", "sender": "Bob",
                 "text": None, "media_type": "audio", "media_file": "PTT-001.opus",
                 "transcript": "Hey how are you", "description": None}]
        result = format_messages_raw(msgs)
        assert "(audio: PTT-001.opus)" in result
        assert "[transcript] Hey how are you" in result

    def test_image_with_description(self):
        msgs = [{"timestamp": "2025-10-01T11:00:00", "sender": "Alice",
                 "text": None, "media_type": "image", "media_file": "IMG-001.jpg",
                 "transcript": None, "description": "A sunset over the beach"}]
        result = format_messages_raw(msgs)
        assert "(image: IMG-001.jpg)" in result
        assert "[description] A sunset over the beach" in result

    def test_empty_list(self):
        assert format_messages_raw([]) == ""

    def test_multiple_messages_separated(self):
        msgs = [
            {"timestamp": "2025-10-01T09:00:00", "sender": "Alice",
             "text": "Hi", "media_type": None, "media_file": None,
             "transcript": None, "description": None},
            {"timestamp": "2025-10-01T09:01:00", "sender": "Bob",
             "text": "Hello", "media_type": None, "media_file": None,
             "transcript": None, "description": None},
        ]
        result = format_messages_raw(msgs)
        assert result.count("[2025-10-01") == 2


class TestGetByDateFallback:
    """Test that get_by_date handles old DBs missing transcript/description columns."""

    def test_old_db_without_transcript_columns(self, tmp_path):
        import sqlite3
        from wain.query import get_by_date
        from wain import config

        db_path = str(tmp_path / "old.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                timestamp TEXT, date TEXT, sender TEXT, raw_sender TEXT,
                text TEXT, media_file TEXT, media_type TEXT,
                media_path TEXT, chunk_id INTEGER, notes TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE chunks (
                id INTEGER PRIMARY KEY,
                date_start TEXT, date_end TEXT,
                msg_start_id INTEGER, msg_end_id INTEGER,
                message_count INTEGER, summary TEXT,
                embedding_id INTEGER, notes TEXT
            )
        """)
        conn.execute("""
            INSERT INTO messages (id, timestamp, date, sender, text)
            VALUES (1, '2025-10-01T09:00:00', '2025-10-01', 'Alice', 'Hello')
        """)
        conn.commit()
        conn.close()

        original = config.active_db_path
        config.active_db_path = lambda: db_path
        try:
            result = get_by_date("2025-10-01")
            assert len(result["messages"]) == 1
            assert result["messages"][0]["text"] == "Hello"
            assert result["messages"][0]["transcript"] is None
            assert result["messages"][0]["description"] is None
        finally:
            config.active_db_path = original


class TestFilterByDateRange:
    RESULTS = [
        {"date_start": "2025-10-01", "chunk_id": 1},
        {"date_start": "2025-10-05", "chunk_id": 2},
        {"date_start": "2025-10-10", "chunk_id": 3},
        {"date_start": "2025-10-15", "chunk_id": 4},
    ]

    def test_both_bounds(self):
        filtered = filter_by_date_range(self.RESULTS, "2025-10-05", "2025-10-10")
        assert len(filtered) == 2
        assert filtered[0]["chunk_id"] == 2
        assert filtered[1]["chunk_id"] == 3

    def test_from_only(self):
        filtered = filter_by_date_range(self.RESULTS, "2025-10-10", None)
        assert len(filtered) == 2

    def test_to_only(self):
        filtered = filter_by_date_range(self.RESULTS, None, "2025-10-05")
        assert len(filtered) == 2

    def test_no_bounds_passes_through(self):
        filtered = filter_by_date_range(self.RESULTS, None, None)
        assert len(filtered) == 4

    def test_no_matches(self):
        filtered = filter_by_date_range(self.RESULTS, "2026-01-01", "2026-12-31")
        assert len(filtered) == 0

    def test_date_key_fallback(self):
        """Results with 'date' instead of 'date_start' still filter correctly."""
        results = [{"date": "2025-10-01"}, {"date": "2025-10-15"}]
        filtered = filter_by_date_range(results, "2025-10-01", "2025-10-01")
        assert len(filtered) == 1

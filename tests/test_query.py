"""Tests for wain.query -- filtering and formatting."""

import json
from wain.query import filter_by_date_range, format_compact, format_summary_markdown, parse_summary


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

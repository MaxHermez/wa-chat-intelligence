"""Tests for wain.chunker -- daily message grouping."""

from wain.chunker import update_chunks, get_messages_by_day, format_chunk_for_summary


class TestUpdateChunks:
    def test_creates_chunks_for_new_data(self, populated_db):
        conn, _ = populated_db
        created, extended, skipped = update_chunks(conn)
        assert created == 2
        assert extended == 0
        assert skipped == 0

    def test_idempotent_rerun(self, populated_db):
        conn, _ = populated_db
        update_chunks(conn)
        created, extended, skipped = update_chunks(conn)
        assert created == 0
        assert extended == 0
        assert skipped == 2

    def test_chunks_have_correct_message_counts(self, populated_db):
        conn, _ = populated_db
        update_chunks(conn)
        c = conn.cursor()
        rows = c.execute(
            "SELECT date_start, message_count FROM chunks ORDER BY date_start"
        ).fetchall()
        assert rows[0] == ("2025-10-01", 5)
        assert rows[1] == ("2025-10-02", 2)

    def test_messages_tagged_with_chunk_id(self, populated_db):
        conn, _ = populated_db
        update_chunks(conn)
        c = conn.cursor()
        untagged = c.execute(
            "SELECT COUNT(*) FROM messages WHERE chunk_id IS NULL"
        ).fetchone()[0]
        assert untagged == 0

    def test_extend_on_new_messages_same_day(self, populated_db):
        """Adding messages to an existing day's chunk triggers extend, not create."""
        conn, _ = populated_db
        update_chunks(conn)

        # Simulate a new message arriving on day 2
        c = conn.cursor()
        c.execute("""
            INSERT INTO messages (timestamp, date, sender, raw_sender, text)
            VALUES ('2025-10-02T09:00:00', '2025-10-02', 'Alice', 'Alice', 'One more thing')
        """)
        conn.commit()

        created, extended, skipped = update_chunks(conn)
        assert created == 0
        assert extended == 1
        assert skipped == 1

        # Verify the chunk was updated, summary cleared for re-processing
        row = c.execute(
            "SELECT message_count, summary FROM chunks WHERE date_start = '2025-10-02'"
        ).fetchone()
        assert row[0] == 3
        assert row[1] is None


class TestGetMessagesByDay:
    def test_groups_by_date(self, populated_db):
        conn, _ = populated_db
        by_day = get_messages_by_day(conn)
        assert "2025-10-01" in by_day
        assert "2025-10-02" in by_day
        assert len(by_day["2025-10-01"]) == 5
        assert len(by_day["2025-10-02"]) == 2


class TestFormatChunkForSummary:
    def test_text_message(self):
        msgs = [{"timestamp": "2025-10-01T09:00:00", "sender": "Alice",
                 "raw_sender": "Alice", "text": "Hello!", "media_file": None,
                 "media_type": None, "transcript": None, "description": None}]
        result = format_chunk_for_summary(msgs)
        assert "[2025-10-01T09:00] Alice: Hello!" in result

    def test_voice_note_with_transcript(self):
        msgs = [{"timestamp": "2025-10-01T09:00:00", "sender": "Alice",
                 "raw_sender": "Alice", "text": None, "media_file": "voice.opus",
                 "media_type": "audio", "transcript": "Hey how are you", "description": None}]
        result = format_chunk_for_summary(msgs)
        assert "[Voice note: Hey how are you]" in result

    def test_image_with_description(self):
        msgs = [{"timestamp": "2025-10-01T09:00:00", "sender": "Bob",
                 "raw_sender": "Bob", "text": None, "media_file": "photo.jpg",
                 "media_type": "image", "transcript": None, "description": "A sunset over the ocean"}]
        result = format_chunk_for_summary(msgs)
        assert "[Image: A sunset over the ocean]" in result

    def test_media_without_transcript(self):
        msgs = [{"timestamp": "2025-10-01T09:00:00", "sender": "Alice",
                 "raw_sender": "Alice", "text": None, "media_file": "voice.opus",
                 "media_type": "audio", "transcript": None, "description": None}]
        result = format_chunk_for_summary(msgs)
        assert "[audio: voice.opus]" in result

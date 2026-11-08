"""
Assistant conversation timestamps are serialized with their UTC offset.

SQLite drops the timezone of the stored UTC datetimes; without an explicit
offset the browser parses them as local time and shows history shifted by the
UTC offset (e.g. 12:08 AM instead of 01:08 AM in CET).

Run with: python -m pytest test_assistant_database_timestamps.py
"""

from datetime import datetime, timezone

from server.services import assistant_database as db


def _assert_utc_iso(value: str) -> None:
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() is not None, value
    assert parsed.utcoffset().total_seconds() == 0, value
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 120


def test_conversation_and_message_timestamps_carry_utc_offset(tmp_path):
    (tmp_path / ".autoforge").mkdir()
    try:
        conv = db.create_conversation(tmp_path, "demo", "t")
        db.add_message(tmp_path, conv.id, "user", "hi")

        listed = db.get_conversations(tmp_path, "demo")[0]
        full = db.get_conversation(tmp_path, conv.id)
        messages = db.get_messages(tmp_path, conv.id)

        for value in (listed["created_at"], listed["updated_at"], full["created_at"], full["updated_at"],
                      full["messages"][0]["timestamp"], messages[0]["timestamp"]):
            _assert_utc_iso(value)
    finally:
        db.dispose_engine(tmp_path)

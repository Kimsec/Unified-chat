import tempfile
import unittest
from pathlib import Path

from unified_chat.models import UnifiedMessage
from unified_chat.store import MessageStore


class MessageStoreTest(unittest.TestCase):
    def test_deduplicates_platform_message_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MessageStore(Path(temp_dir) / "messages.db")
            payload = UnifiedMessage(
                id="twitch:123",
                platform="twitch",
                platform_message_id="123",
                channel_id="1",
                author_display_name="Kim",
                author_login="kim",
                text="hello",
                sent_at="2026-04-04T20:00:00+00:00",
            )
            self.assertTrue(store.add_message(payload))
            self.assertFalse(store.add_message(payload))
            self.assertEqual(len(store.list_messages()), 1)
            store.close()

    def test_keeps_only_latest_500_messages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MessageStore(Path(temp_dir) / "messages.db")
            for index in range(501):
                payload = UnifiedMessage(
                    id=f"twitch:{index}",
                    platform="twitch",
                    platform_message_id=str(index),
                    channel_id="1",
                    author_display_name="Kim",
                    author_login="kim",
                    text=f"hello {index}",
                    sent_at=f"2026-04-04T20:{index // 60:02d}:{index % 60:02d}+00:00",
                )
                self.assertTrue(store.add_message(payload))

            messages = store.list_messages(limit=500)
            self.assertEqual(len(messages), 500)
            self.assertEqual(messages[0].platform_message_id, "1")
            self.assertEqual(messages[-1].platform_message_id, "500")
            store.close()

    def test_persists_twitch_system_notice_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MessageStore(Path(temp_dir) / "messages.db")
            payload = UnifiedMessage(
                id="twitch:notice:1",
                platform="twitch",
                platform_message_id="notice-1",
                message_kind="system",
                notice_type="sub",
                channel_id="1",
                author_display_name="musYo",
                author_login="musyo",
                text="musYo gifted a Tier 1 sub to TouchOfMadness7!",
                sent_at="2026-04-06T18:00:00+00:00",
            )

            self.assertTrue(store.add_message(payload))

            messages = store.list_messages(limit=10)
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0].message_kind, "system")
            self.assertEqual(messages[0].notice_type, "sub")
            self.assertEqual(messages[0].text, "musYo gifted a Tier 1 sub to TouchOfMadness7!")
            store.close()


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

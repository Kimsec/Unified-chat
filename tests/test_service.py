from datetime import datetime
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unified_chat.service import ChatService
from unified_chat.store import MessageStore


class ChatServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MessageStore(Path(self.temp_dir.name) / "messages.db")
        self.service = ChatService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    async def test_bootstrap_event_always_includes_hype_train_key(self):
        payload = await self.service.bootstrap_event()

        self.assertIn("hype_train", payload)
        self.assertIsNone(payload["hype_train"])

    def test_ended_hype_train_survives_until_grace_period_expires(self):
        ended = {
            "type": "hype_train",
            "id": "train-1",
            "phase": "end",
            "level": 3,
            "progress": 80,
            "goal": 100,
            "total": 80,
            "ended_at": "2026-04-06T20:00:00+00:00",
        }

        with mock.patch("unified_chat.service.time.monotonic", return_value=100.0):
            self.service.set_hype_train(ended)

        with mock.patch("unified_chat.service.time.monotonic", return_value=104.9):
            current = self.service.get_hype_train()

        self.assertIsNotNone(current)
        self.assertEqual(current["phase"], "end")
        self.assertEqual(current["progress"], 80)
        self.assertLessEqual(current["hide_after_ms"], 100)

    def test_ended_hype_train_clears_after_grace_period(self):
        ended = {
            "type": "hype_train",
            "id": "train-2",
            "phase": "end",
            "level": 2,
            "progress": 50,
            "goal": 100,
            "total": 50,
            "ended_at": "2026-04-06T20:05:00+00:00",
        }

        with mock.patch("unified_chat.service.time.monotonic", return_value=100.0):
            self.service.set_hype_train(ended)

        with mock.patch("unified_chat.service.time.monotonic", return_value=105.1):
            current = self.service.get_hype_train()

        self.assertIsNone(current)

    def test_active_hype_train_clears_when_expires_at_has_passed(self):
        active = {
            "type": "hype_train",
            "id": "train-3",
            "phase": "progress",
            "level": 4,
            "progress": 25,
            "goal": 100,
            "total": 25,
            "expires_at": "2026-04-06T20:10:00+00:00",
        }

        with mock.patch("unified_chat.service.utcnow") as utcnow_mock:
            utcnow_mock.return_value = datetime.fromisoformat("2026-04-06T20:09:00+00:00")
            self.service.set_hype_train(active)
            utcnow_mock.return_value = datetime.fromisoformat("2026-04-06T20:11:00+00:00")
            current = self.service.get_hype_train()

        self.assertIsNone(current)


if __name__ == "__main__":
    unittest.main()

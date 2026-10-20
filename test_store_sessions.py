import unittest

from store import is_bot_session_expired


class BotSessionTimeoutTests(unittest.TestCase):
    def test_session_expires_at_five_minutes_of_inactivity(self):
        self.assertFalse(is_bot_session_expired(updated_at=700, now=999))
        self.assertTrue(is_bot_session_expired(updated_at=700, now=1000))

    def test_recent_message_keeps_session_active(self):
        self.assertFalse(is_bot_session_expired(updated_at=700, now=850))


if __name__ == "__main__":
    unittest.main()

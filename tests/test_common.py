import unittest
from unittest.mock import Mock, patch

import common


class RetryTests(unittest.TestCase):
    @patch("common.time.sleep")
    @patch("common.requests.post")
    def test_retries_429_then_returns_success(self, post, sleep):
        limited = Mock(status_code=429)
        limited.json.return_value = {"parameters": {"retry_after": 2}}
        success = Mock(status_code=200)
        post.side_effect = [limited, success]
        result = common.post_with_retry("https://example.invalid", json={"ok": True})
        self.assertIs(result, success)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(2)

    @patch("common.time.sleep")
    @patch("common.requests.post")
    def test_does_not_retry_timeout_exception(self, post, sleep):
        import requests
        post.side_effect = requests.Timeout("uncertain delivery")
        with self.assertRaises(requests.Timeout):
            common.post_with_retry("https://example.invalid")
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    @patch("common.time.sleep")
    @patch("common.requests.post")
    def test_does_not_retry_telegram_5xx(self, post, sleep):
        unavailable = Mock(status_code=503)
        post.return_value = unavailable
        result = common.post_with_retry(
            "https://api.telegram.org/botTOKEN/sendMessage",
            json={"chat_id": "@channel", "text": "test"},
        )
        self.assertIs(result, unavailable)
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    @patch("common.time.sleep")
    @patch("common.requests.post")
    def test_retries_non_telegram_5xx(self, post, sleep):
        unavailable = Mock(status_code=503)
        success = Mock(status_code=200)
        post.side_effect = [unavailable, success]
        result = common.post_with_retry("https://generativelanguage.googleapis.com/test")
        self.assertIs(result, success)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(5)


if __name__ == "__main__":
    unittest.main()

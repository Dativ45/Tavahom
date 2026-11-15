import tempfile, unittest
from pathlib import Path
from unittest.mock import Mock
import requests
from desk_core import DeskError, DeskState, DeliveryUncertain, canonical_url, format_rss_notice, item_key, safe_error, telegram_send

class CoreTests(unittest.TestCase):
    def test_key_normalization(self):
        self.assertEqual(item_key(" A  B ", "https://EXAMPLE.com/a/#x"), item_key("a b", "https://example.com/a"))
    def test_reject_non_http_url(self):
        self.assertEqual(canonical_url("javascript:alert(1)"), "")
    def test_state_lifecycle(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"state.json"; s=DeskState(p); k=item_key("x","https://e.test/x")
            s.begin(k,"x"); self.assertEqual(s.status(k),"pending"); s.mark_sent(k,12)
            self.assertEqual(DeskState(p).status(k),"sent")
            with self.assertRaises(DeskError): s.begin(k)
    def test_corrupt_state_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"state.json"; p.write_text("{broken")
            with self.assertRaises(DeskError): DeskState(p)
            self.assertEqual(p.read_text(),"{broken")
    def test_rss_notice(self):
        self.assertIn("فقط بر پایه تیتر",format_rss_notice(""))
        self.assertIn("خوراک RSS",format_rss_notice("excerpt"))
    def test_secret_redaction(self):
        result=safe_error("https://api.telegram.org/bot123:ABC/sendMessage","123:ABC")
        self.assertNotIn("123:ABC",result)
    def test_telegram_success(self):
        response=Mock(status_code=200,ok=True); response.json.return_value={"ok":True,"result":{"message_id":7}}
        session=Mock(); session.post.return_value=response
        self.assertEqual(telegram_send("hi",bot_token="t",chat_id="@c",session=session)["message_id"],7)
        session.post.assert_called_once()
    def test_telegram_200_ok_false(self):
        response=Mock(status_code=200,ok=True); response.json.return_value={"ok":False,"description":"chat not found"}
        session=Mock(); session.post.return_value=response
        with self.assertRaises(DeskError): telegram_send("hi",bot_token="t",chat_id="@c",session=session)
    def test_timeout_is_not_retried(self):
        session=Mock(); session.post.side_effect=requests.Timeout()
        with self.assertRaises(DeliveryUncertain): telegram_send("hi",bot_token="t",chat_id="@c",session=session)
        session.post.assert_called_once()

if __name__=="__main__": unittest.main()

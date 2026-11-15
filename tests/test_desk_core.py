import tempfile, unittest
from pathlib import Path
from unittest.mock import Mock, patch
import requests
from unittest.mock import patch
from tech_digest_v2 import translate
from desk_core import DeskError, DeskState, DeliveryUncertain, canonical_url, deduplicate_items, format_rss_notice, item_key, safe_error, telegram_send, telegram_send_photo, fetch_feed, title_similarity

class CoreTests(unittest.TestCase):
    def test_key_normalization(self):
        self.assertEqual(item_key(" A  B ", "https://EXAMPLE.com/a/#x"), item_key("a b", "https://example.com/a"))
    def test_reject_non_http_url(self):
        self.assertEqual(canonical_url("javascript:alert(1)"), "")
    def test_tracking_parameters_do_not_change_url_identity(self):
        self.assertEqual(canonical_url("https://EXAMPLE.com/story?utm_source=x&id=4#top"),
                         canonical_url("https://example.com/story?id=4"))
    def test_conservative_near_duplicate_detection(self):
        a = {"title":"OpenAI announces a major new artificial intelligence model today", "link":"https://a.test/1", "published_at":20}
        b = {"title":"OpenAI announces major new artificial intelligence model today", "link":"https://b.test/2", "published_at":10}
        c = {"title":"Football team wins championship after final match", "link":"https://c.test/3", "published_at":5}
        unique, duplicates = deduplicate_items([b, c, a], threshold=0.88)
        self.assertEqual(len(unique), 2)
        self.assertEqual(len(duplicates), 1)
        self.assertGreater(title_similarity(a["title"], b["title"]), 0.88)
    def test_short_headline_containment_is_not_duplicate(self):
        self.assertLess(title_similarity("Apple event", "Apple event changes global technology market"), 0.90)
    def test_state_lifecycle(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"state.json"; s=DeskState(p); k=item_key("x","https://e.test/x")
            s.begin(k,"x",metadata={"source":"Example","published_at":100,"fetched_at":110})
            self.assertEqual(s.status(k),"pending")
            with patch("desk_core.time.time", return_value=150):
                s.mark_sent(k,12)
            reloaded=DeskState(p)
            self.assertEqual(reloaded.status(k),"sent")
            self.assertEqual(reloaded.data["items"][k]["publish_delay_seconds"],50)
            self.assertEqual(reloaded.data["items"][k]["source"],"Example")
            with self.assertRaises(DeskError): s.begin(k)
    def test_legacy_seen_import(self):
        with tempfile.TemporaryDirectory() as d:
            state=DeskState(Path(d)/"state.json")
            state.import_legacy_seen(["old-key"])
            self.assertEqual(state.status("legacy:old-key"),"sent")
    def test_corrupt_state_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"state.json"; p.write_text("{broken")
            with self.assertRaises(DeskError): DeskState(p)
            self.assertEqual(p.read_text(),"{broken")
    def test_translation_rejects_missing_summary(self):
        item={"source":"Example","title":"A technology headline","summary":"Source excerpt"}
        with patch("tech_digest_v2.gemini_json", return_value={"title_fa":"عنوان فارسی معتبر","summary_fa":"","category":"هوش مصنوعی"}):
            with self.assertRaises(DeskError):
                translate(item)
    def test_translation_accepts_complete_result(self):
        item={"source":"Example","title":"A technology headline","summary":"Source excerpt"}
        result={"title_fa":"عنوان فارسی معتبر","summary_fa":"این یک خلاصه فارسی معتبر و کافی است.","category":"هوش مصنوعی"}
        with patch("tech_digest_v2.gemini_json", return_value=result):
            title, summary, category=translate(item)
        self.assertEqual(title,result["title_fa"])
        self.assertEqual(summary,result["summary_fa"])
        self.assertEqual(category,"هوش مصنوعی")
    def test_rss_notice(self):
        self.assertIn("بر پایه تیتر",format_rss_notice(""))
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
    def test_photo_send_success(self):
        response=Mock(status_code=200,ok=True); response.json.return_value={"ok":True,"result":{"message_id":8}}
        session=Mock(); session.post.return_value=response
        result=telegram_send_photo("https://example.com/photo.jpg","caption",bot_token="t",chat_id="@c",session=session)
        self.assertEqual(result["message_id"],8)
        session.post.assert_called_once()
    def test_photo_caption_limit(self):
        with self.assertRaises(DeskError):
            telegram_send_photo("https://example.com/photo.jpg","x"*1025,bot_token="t",chat_id="@c",session=Mock())
    def test_feed_keeps_image_and_timestamp(self):
        xml=b"""<rss version="2.0"><channel><title>x</title><item><title>Story</title><link>https://example.com/a</link><description>Summary</description><enclosure url="https://example.com/a.jpg" type="image/jpeg"/><pubDate>Sat, 15 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
        response=Mock(content=xml); response.raise_for_status.return_value=None
        session=Mock(); session.get.return_value=response
        item=fetch_feed("https://example.com/feed",session=session)[0]
        self.assertEqual(item["image"],"https://example.com/a.jpg")
        self.assertGreater(item["published_at"],0)
    def test_timeout_is_not_retried(self):
        session=Mock(); session.post.side_effect=requests.Timeout()
        with self.assertRaises(DeliveryUncertain): telegram_send("hi",bot_token="t",chat_id="@c",session=session)
        session.post.assert_called_once()

if __name__=="__main__": unittest.main()

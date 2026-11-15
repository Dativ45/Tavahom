import unittest

import trend_radar


class TrendRadarTests(unittest.TestCase):
    def test_clusters_only_multi_domain_coverage(self):
        articles = [
            {
                "title": "NASA announces new lunar mission",
                "url": "https://space.example/a",
                "domain": "space.example",
                "seen_at": "2026-11-15T18:00:00+00:00",
                "query_category": "علم و محیط زیست",
            },
            {
                "title": "NASA reveals new lunar mission plans",
                "url": "https://science.example/b",
                "domain": "science.example",
                "seen_at": "2026-11-15T18:05:00+00:00",
                "query_category": "علم و محیط زیست",
            },
            {
                "title": "Taylor Swift announces a new album",
                "url": "https://music.example/c",
                "domain": "music.example",
                "seen_at": "2026-11-15T18:06:00+00:00",
                "query_category": "فرهنگ",
            },
        ]
        candidates = trend_radar.cluster_gdelt_articles(articles)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(set(candidates[0]["domains"]), {"space.example", "science.example"})

    def test_rejects_non_commercial_or_unclear_license(self):
        image = {
            "id": "image-1",
            "title": "NASA lunar mission rocket",
            "tags": [{"name": "NASA"}, {"name": "lunar"}],
            "license": "by-nc",
            "license_url": "https://creativecommons.org/licenses/by-nc/4.0/",
            "foreign_landing_url": "https://source.example/image",
            "url": "https://cdn.example/image.jpg",
            "filetype": "jpg",
            "width": 1200,
            "height": 800,
            "mature": False,
        }
        self.assertFalse(trend_radar.usable_openverse_image(image, "NASA lunar mission"))
        image["license"] = "by"
        image["license_url"] = ""
        self.assertFalse(trend_radar.usable_openverse_image(image, "NASA lunar mission"))

    def test_accepts_relevant_cc_by_image_with_source_and_license_links(self):
        image = {
            "id": "image-2",
            "title": "NASA lunar mission rocket",
            "tags": [{"name": "NASA"}, {"name": "lunar"}],
            "license": "by",
            "license_version": "4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "foreign_landing_url": "https://source.example/image",
            "url": "https://cdn.example/image.jpg",
            "filetype": "jpg",
            "width": 1200,
            "height": 800,
            "mature": False,
        }
        self.assertTrue(trend_radar.usable_openverse_image(image, "NASA lunar mission"))

    def test_rejects_irrelevant_or_mature_image(self):
        image = {
            "id": "image-3",
            "title": "Beach holiday",
            "tags": [{"name": "ocean"}],
            "license": "cc0",
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "foreign_landing_url": "https://source.example/image",
            "url": "https://cdn.example/image.jpg",
            "filetype": "jpg",
            "width": 1200,
            "height": 800,
            "mature": False,
        }
        self.assertFalse(trend_radar.usable_openverse_image(image, "NASA lunar mission"))
        image["title"] = "NASA lunar mission rocket"
        image["mature"] = True
        self.assertFalse(trend_radar.usable_openverse_image(image, "NASA lunar mission"))

    def test_caption_discloses_archive_image_and_attribution(self):
        item = {
            "headline_fa": "آغاز مأموریت تازه ماه",
            "explanation_fa": "این موضوع در چند رسانه پوشش داده شده است.",
            "domains": ["space.example", "science.example"],
            "articles": [{
                "url": "https://space.example/story",
                "domain": "space.example",
            }],
            "caution_fa": "",
        }
        image = {
            "title": "NASA lunar mission",
            "creator": "Jane Creator",
            "creator_url": "https://creator.example",
            "license": "by",
            "license_version": "4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "foreign_landing_url": "https://source.example/image",
        }
        caption = trend_radar.build_caption(item, image)
        self.assertIn("تصویر آرشیوی/تزئینی", caption)
        self.assertIn("Jane Creator", caption)
        self.assertIn("CC BY 4.0", caption)
        self.assertIn("space.example", caption)

    def test_daily_cap_counts_only_current_tehran_date(self):
        state = {
            "images_posted": [
                {"date": "2026-11-15", "image_id": "a"},
                {"date": "2026-11-15", "image_id": "b"},
                {"date": "2026-11-14", "image_id": "c"},
            ]
        }
        self.assertEqual(trend_radar.daily_image_count(state, "2026-11-15"), 2)

    def test_topic_duplicate_is_detected(self):
        state = {
            "images_posted": [
                {"topic_key": "2026-11-15:abc", "image_id": "image-1"}
            ]
        }
        item = {"fingerprint": "abc"}
        self.assertTrue(
            trend_radar.already_posted(state, "2026-11-15", item, {"id": "image-2"})
        )


    def test_rejects_unsafe_family_content(self):
        unsafe = {
            "topic": "Breaking shooting incident",
            "articles": [{"title": "Police investigate shooting"}],
        }
        safe = {
            "topic": "NASA lunar mission",
            "articles": [{"title": "New lunar mission plans"}],
        }
        self.assertFalse(trend_radar.candidate_is_safe(unsafe))
        self.assertTrue(trend_radar.candidate_is_safe(safe))

    def test_caption_stays_within_telegram_limit(self):
        item = {
            "headline_fa": "آغاز مأموریت تازه ماه " * 12,
            "explanation_fa": "این موضوع در چند رسانه پوشش داده شده است. " * 20,
            "domains": ["space.example", "science.example"],
            "articles": [
                {"url": "https://space.example/story", "domain": "space.example"},
                {"url": "https://science.example/story", "domain": "science.example"},
            ],
            "caution_fa": "توضیح احتیاطی " * 20,
        }
        image = {
            "title": "NASA lunar mission rocket " * 8,
            "creator": "Jane Creator " * 8,
            "creator_url": "https://creator.example/profile",
            "license": "by",
            "license_version": "4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "foreign_landing_url": "https://source.example/image",
        }
        caption = trend_radar.build_caption(item, image)
        self.assertLessEqual(len(caption), 1024)


if __name__ == "__main__":
    unittest.main()

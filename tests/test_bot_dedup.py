import os
import unittest

# Importing the publisher must not require production Telegram credentials in tests.
os.environ.setdefault("STATUS_ROOM_DRY_RUN", "true")
os.environ.setdefault("GEMINI_API_KEY", "test-only-not-a-real-key")

import bot


def story(key, title, domain, published_ts=100, summary=""):
    return {
        "key": key,
        "title": title,
        "domain": domain,
        "source": domain,
        "link": f"https://{domain}/story/{key}",
        "published_ts": published_ts,
        "summary": summary,
    }


class StoryDeskRoutingTests(unittest.TestCase):
    def test_chemical_weapons_plot_cannot_be_classified_as_livelihood(self):
        item = story(
            "chemical-plot",
            "Iran rejects report of chemical weapons plot in Washington",
            "example.com",
            summary="Iran rejects report of a plot to use chemical weapons in Washington.",
        )
        self.assertEqual(bot.deterministic_desk_override(item, "معیشت"), "جنگ و امنیت")

    def test_dirty_bomb_plot_is_routed_to_security(self):
        item = story(
            "dirty-bomb",
            "Officials investigate alleged dirty bomb plot",
            "example.com",
        )
        self.assertEqual(bot.deterministic_desk_override(item, "اقتصاد"), "جنگ و امنیت")

    def test_ordinary_livelihood_story_keeps_model_category(self):
        item = story(
            "wages",
            "Workers demand higher wages amid rising living costs",
            "example.com",
        )
        self.assertEqual(bot.deterministic_desk_override(item, "معیشت"), "معیشت")


class StoryDeduplicationTests(unittest.TestCase):
    def test_syndicated_copies_become_one_story(self):
        first = story(
            "one",
            "Central bank cuts interest rates by 0.25 percentage points",
            "news-a.example",
            200,
        )
        second = story(
            "two",
            "Central bank cuts interest rates by a quarter point",
            "news-b.example",
            190,
        )

        result = bot.cluster_stories([second, first])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["key"], "one")
        self.assertEqual([item["key"] for item in result[0]["_related"]], ["two"])

    def test_unrelated_events_remain_separate(self):
        first = story(
            "one",
            "Central bank cuts interest rates by 0.25 percentage points",
            "news-a.example",
        )
        second = story(
            "two",
            "NASA launches a new climate observation satellite",
            "science.example",
        )

        self.assertEqual(len(bot.cluster_stories([first, second])), 2)

    def test_same_publisher_is_not_counted_as_independent_coverage(self):
        first = story(
            "one",
            "Central bank cuts interest rates by 0.25 percentage points",
            "news-a.example",
        )
        second = story(
            "two",
            "Central bank cuts interest rates by a quarter point",
            "news-a.example",
            90,
        )

        result = bot.cluster_stories([first, second])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["_related"], [])


if __name__ == "__main__":
    unittest.main()

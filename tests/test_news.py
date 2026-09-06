"""STEP7: ニュース取得パイプラインのテスト。ネットワークアクセスは一切行わない。
AI APIも使用しない（categorize()はルールベースの辞書引きのみ）。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from data import news

JST = ZoneInfo("Asia/Tokyo")

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>"半導体" - Google ニュース</title>
<item>
  <title>半導体メモリー急伸 - 日本経済新聞</title>
  <link>https://news.google.com/rss/articles/AAA?oc=5</link>
  <pubDate>Sun, 06 Sep 2026 12:00:00 GMT</pubDate>
  <source url="https://nikkei.com">日本経済新聞</source>
</item>
<item>
  <title>半導体株が上昇 半導体メモリー急伸で連想買い - ロイター</title>
  <link>https://news.google.com/rss/articles/BBB?oc=5</link>
  <pubDate>Sun, 06 Sep 2026 11:30:00 GMT</pubDate>
  <source url="https://reuters.com">ロイター</source>
</item>
<item>
  <title>まったく無関係な記事の見出し - 共同通信</title>
  <link>https://news.google.com/rss/articles/CCC?oc=5</link>
  <pubDate>Fri, 04 Sep 2026 00:00:00 GMT</pubDate>
  <source url="https://kyodo.co.jp">共同通信</source>
</item>
</channel></rss>"""


def _fake_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.text = text
    return resp


# --- RSS取得・パース --------------------------------------------------------

def test_fetch_keyword_news_parses_title_url_source_pubdate():
    with patch.object(news.requests, "get", return_value=_fake_response(SAMPLE_RSS)):
        items, error = news.fetch_keyword_news("半導体", max_items=10)

    assert error is None
    assert len(items) == 3
    first = items[0]
    assert first["title"] == "半導体メモリー急伸"  # ソース名の重複サフィックスが除去されている
    assert first["url"] == "https://news.google.com/rss/articles/AAA?oc=5"
    assert first["source"] == "日本経済新聞"
    assert first["pub_date"] == "Sun, 06 Sep 2026 12:00:00 GMT"


def test_fetch_keyword_news_respects_max_items():
    with patch.object(news.requests, "get", return_value=_fake_response(SAMPLE_RSS)):
        items, _ = news.fetch_keyword_news("半導体", max_items=2)
    assert len(items) == 2


def test_fetch_keyword_news_network_failure_returns_no_fabricated_items():
    with patch.object(news.requests, "get", side_effect=ConnectionError("network down")):
        items, error = news.fetch_keyword_news("半導体", max_items=10)
    assert items == []
    assert error is not None
    assert "network down" in error


# --- 公開日時の処理・JST変換 -------------------------------------------------

def test_to_jst_iso_converts_gmt_pubdate_to_jst_offset():
    iso = news._to_jst_iso("Sun, 06 Sep 2026 12:00:00 GMT")
    assert iso == "2026-09-06T21:00:00+09:00"  # GMT(UTC) 12:00 -> JST 21:00 同日


def test_to_jst_iso_returns_none_for_missing_or_unparseable():
    assert news._to_jst_iso(None) is None
    assert news._to_jst_iso("not a date") is None


# --- 24時間フィルタ（取得日時と公開日時を混同しない） -------------------------

def _item(published_at: str | None, url: str = "https://example.com/a") -> news.NewsItem:
    return news.NewsItem(
        title="t", url=url, published_at=published_at, source="s", category="other", matched_keyword="k"
    )


def test_filter_recent_keeps_within_24h_and_drops_older():
    now = datetime(2026, 9, 6, 21, 0, tzinfo=JST)
    within = _item("2026-09-06T10:00:00+09:00", url="https://example.com/within")  # 11h前
    boundary = _item("2026-09-05T21:00:00+09:00", url="https://example.com/boundary")  # ちょうど24h前
    too_old = _item("2026-09-05T20:00:00+09:00", url="https://example.com/old")  # 25h前
    future = _item("2026-09-06T22:00:00+09:00", url="https://example.com/future")  # fetched_atより未来

    result = news.filter_recent([within, boundary, too_old, future], now, hours=24)
    urls = {i.url for i in result}
    assert "https://example.com/within" in urls
    assert "https://example.com/boundary" in urls
    assert "https://example.com/old" not in urls
    assert "https://example.com/future" not in urls


def test_filter_recent_drops_items_with_unknown_published_at_instead_of_guessing():
    now = datetime(2026, 9, 6, 21, 0, tzinfo=JST)
    result = news.filter_recent([_item(None)], now, hours=24)
    assert result == []


# --- テーマ分類（ルールベース） ----------------------------------------------

def test_categorize_maps_known_keywords():
    assert news.categorize("NVIDIA") == "nvidia"
    assert news.categorize("AI") == "ai"
    assert news.categorize("半導体") == "semiconductor"
    assert news.categorize("日銀") == "boj"
    assert news.categorize("為替") == "fx"
    assert news.categorize("金利") == "rates"
    assert news.categorize("米金融政策") == "us_market"
    assert news.categorize("日本株") == "japan_market"
    assert news.categorize("中国") == "china"
    assert news.categorize("原油・エネルギー") == "energy"
    assert news.categorize("防衛") == "defense"


def test_categorize_unknown_keyword_falls_back_to_other():
    assert news.categorize("未知のキーワード") == "other"
    assert news.categorize("未知のキーワード") in news.VALID_CATEGORIES


# --- 重複除去 --------------------------------------------------------------

def test_dedupe_removes_exact_url_duplicate():
    a = _item("2026-09-06T21:00:00+09:00", url="https://example.com/a")
    dup = _item("2026-09-06T21:05:00+09:00", url="https://example.com/a")
    result = news.dedupe([a, dup])
    assert len(result) == 1


def test_dedupe_removes_normalized_url_duplicate_differing_only_by_query():
    a = _item("2026-09-06T21:00:00+09:00", url="https://news.google.com/rss/articles/AAA?oc=5")
    dup = _item("2026-09-06T21:05:00+09:00", url="https://news.google.com/rss/articles/AAA?oc=3")
    result = news.dedupe([a, dup])
    assert len(result) == 1


def test_dedupe_merges_near_identical_titles_from_different_sources():
    a = news.NewsItem(
        title="半導体メモリー急伸", url="https://example.com/a",
        published_at="2026-09-06T21:00:00+09:00", source="日本経済新聞",
        category="semiconductor", matched_keyword="半導体",
    )
    b = news.NewsItem(
        title="半導体株が上昇 半導体メモリー急伸で連想買い", url="https://example.com/b",
        published_at="2026-09-06T20:30:00+09:00", source="ロイター",
        category="semiconductor", matched_keyword="半導体",
    )
    result = news.dedupe([a, b], similarity_threshold=0.4)
    assert len(result) == 1  # 語の重なりが大きいため統合される


def test_dedupe_does_not_over_merge_distinct_articles():
    a = _item("2026-09-06T21:00:00+09:00", url="https://example.com/a")
    a.title = "日銀が政策金利を発表"
    b = _item("2026-09-06T20:00:00+09:00", url="https://example.com/b")
    b.title = "まったく無関係な記事の見出し"
    result = news.dedupe([a, b], similarity_threshold=0.75)
    assert len(result) == 2  # 似ていない記事は残すべき


# --- fetch_news 統合テスト ---------------------------------------------------

SETTINGS = {
    "news": {
        "max_items_per_keyword": 10,
        "max_total_items": 30,
        "recent_hours": 24,
        "dedup_similarity_threshold": 0.75,
        "keywords": ["半導体"],
    }
}


def test_fetch_news_end_to_end_filters_dedupes_and_categorizes():
    now = datetime(2026, 9, 6, 21, 0, tzinfo=JST)
    with patch.object(news.requests, "get", return_value=_fake_response(SAMPLE_RSS)):
        out = news.fetch_news(SETTINGS, now=now)

    assert out["fetched_at"] == now.isoformat()
    assert out["errors"] == []
    # 3件中、無関係記事(2日前)は24時間フィルタで除外、残り2件は類似タイトルなので統合され1件
    assert len(out["news"]) == 1
    assert out["news"][0].category == "semiconductor"


def test_fetch_news_zero_results_is_not_an_error():
    now = datetime(2026, 9, 6, 21, 0, tzinfo=JST)
    empty_rss = '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
    with patch.object(news.requests, "get", return_value=_fake_response(empty_rss)):
        out = news.fetch_news(SETTINGS, now=now)
    assert out["news"] == []
    assert out["errors"] == []


def test_fetch_news_partial_failure_keeps_other_keywords_and_reports_error():
    settings = {
        "news": {
            **SETTINGS["news"],
            "keywords": ["半導体", "AI"],
        }
    }
    now = datetime(2026, 9, 6, 21, 0, tzinfo=JST)

    def fake_get(url, params=None, timeout=None, headers=None):
        if params["q"] == "AI":
            raise ConnectionError("network down")
        return _fake_response(SAMPLE_RSS)

    with patch.object(news.requests, "get", side_effect=fake_get):
        out = news.fetch_news(settings, now=now)

    assert len(out["news"]) == 1  # 半導体側は成功
    assert any("network down" in e for e in out["errors"])

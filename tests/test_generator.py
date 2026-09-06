"""STEP4+STEP5: HTML生成のテスト。ネットワークアクセスは一切行わない。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from analysis.score import compute_score
from data.events import EventRecord
from data.market import MetricResult
from data.news import NewsItem
from generator.html import generate_report

SETTINGS = {
    "score_threshold_percent": 0.2,
    "bond_yield_threshold_bp": 3,
    "news": {
        "category_priority": [
            "nvidia", "semiconductor", "ai", "boj", "fx", "rates",
            "japan_market", "china", "energy", "defense",
        ],
        "max_per_category_display": 3,
        "max_display_total": 10,
    },
}
JST = ZoneInfo("Asia/Tokyo")


def _empty_events_out(now: datetime) -> dict:
    return {
        "events": [],
        "errors": [],
        "window_start": now.date().isoformat(),
        "window_end": (now.date() + timedelta(days=7)).isoformat(),
    }


def _empty_news_out() -> dict:
    return {"news": [], "errors": [], "fetched_at": None}


def _ok(key, label, *, change_percent=None, change_bp=None, value=100.0, timestamp="2026-09-04", source=None):
    return MetricResult(
        key=key,
        label=label,
        status="OK",
        value=value,
        previous_value=value - (change_percent or 0),
        change=change_percent or change_bp or 0,
        change_percent=change_percent,
        change_bp=change_bp,
        source=source or f"test:{key}",
        timestamp=timestamp,
    )


def _all_ok_overseas():
    return {
        "nasdaq": _ok("nasdaq", "Nasdaq総合", value=26506.99, change_percent=-0.29),
        "sox": _ok("sox", "SOX", value=11735.26, change_percent=3.38),
        "us10y": _ok("us10y", "米10年債利回り", value=4.784, change_bp=2.2),
        "usdjpy": _ok("usdjpy", "ドル円", value=155.78, change_percent=-1.82),
        "kospi": _ok("kospi", "KOSPI", value=6687.21, change_percent=1.64),
    }


def _all_ok_japan():
    return {
        "nikkei_futures": _ok(
            "nikkei_futures", "日経225先物", value=65820.0, change_percent=1.77,
            timestamp="2026-09-05", source="yfinance_snapshot:NKD=F@Asia/Tokyo05",
        ),
        "topix_etf": _ok(
            "topix_etf", "TOPIX連動ETF（1306.T）", value=427.5, change_percent=0.07,
            source="yfinance:1306.T",
        ),
        "japan10y": _ok(
            "japan10y", "日本10年国債利回り", value=2.966, change_bp=-4.0, source="mof_jgb10y"
        ),
    }


def _all_ok_results():
    return {"overseas": _all_ok_overseas(), "japan": _all_ok_japan()}


def _generate(tmp_path, results, now=None, events_out=None, news_out=None):
    now = now or datetime(2026, 9, 4, 7, 0, tzinfo=JST)  # 平日固定（テストの再現性のため）
    events_out = events_out if events_out is not None else _empty_events_out(now)
    news_out = news_out if news_out is not None else _empty_news_out()
    score_out = compute_score(results["overseas"], SETTINGS)
    path = generate_report(
        results, score_out, SETTINGS, events_out, news_out, output_dir=tmp_path, now=now
    )
    return path, score_out


def test_generate_report_creates_dated_file_and_index(tmp_path):
    results = _all_ok_results()
    path, _ = _generate(tmp_path, results)
    assert path.exists()
    assert path.name == "2026-09-04.html"
    index_path = tmp_path / "index.html"
    assert index_path.exists()
    assert index_path.read_text(encoding="utf-8") == path.read_text(encoding="utf-8")


def test_date_is_auto_filled(tmp_path):
    results = _all_ok_results()
    path, _ = _generate(tmp_path, results)
    html = path.read_text(encoding="utf-8")
    assert "2026.09.04" in html  # dateline
    assert "FRI" in html


def test_five_scored_metrics_values_and_changes_present(tmp_path):
    results = _all_ok_results()
    path, _ = _generate(tmp_path, results)
    html = path.read_text(encoding="utf-8")
    assert "26,506.99" in html and "0.29%" in html  # Nasdaq
    assert "11,735.26" in html and "3.38%" in html  # SOX
    assert "4.784%" in html and "2.2bp" in html      # US10Y
    assert "155.78" in html and "1.82%" in html      # USD/JPY
    assert "6,687.21" in html and "1.64%" in html     # KOSPI


def test_score_and_total_score_present(tmp_path):
    results = _all_ok_results()
    path, score_out = _generate(tmp_path, results)
    html = path.read_text(encoding="utf-8")
    assert f"{score_out['score']:+d}" in html
    assert score_out["verdict"] in html
    assert "スコア判定: -1" in html  # Nasdaq(-0.29%)は-1のはず
    assert "スコア判定: +1" in html  # SOX(+3.38%)は+1のはず


def test_missing_metric_shows_unavailable_not_fabricated_number(tmp_path):
    results = _all_ok_results()
    results["overseas"]["usdjpy"] = MetricResult(
        key="usdjpy", label="ドル円", status="ERROR", error="no NY17:00 snapshot"
    )
    path, score_out = _generate(tmp_path, results)
    html = path.read_text(encoding="utf-8")
    assert score_out["available"] == 4
    assert score_out["provisional"] is True
    assert "取得失敗" in html
    assert "no NY17:00 snapshot" in html
    assert "4/5指標" in html  # 一部欠損が分かる表示になっている


def test_not_yet_implemented_indicators_show_as_such_not_fake_numbers(tmp_path):
    results = _all_ok_results()
    path, _ = _generate(tmp_path, results)
    html = path.read_text(encoding="utf-8")
    # S&P500/NYダウ/日経平均は現時点で未実装であり、でっち上げ数値を入れない
    assert html.count("未実装") >= 3


def test_no_leftover_placeholder_tokens(tmp_path):
    results = _all_ok_results()
    path, _ = _generate(tmp_path, results)
    html = path.read_text(encoding="utf-8")
    assert re.findall(r"\{\{[A-Z0-9_]+\}\}", html) == []


def test_html_tags_are_balanced_and_well_formed(tmp_path):
    """簡易的な整形チェック（フルHTMLパーサ依存を避けるため主要タグの開閉数を比較）。"""
    results = _all_ok_results()
    path, _ = _generate(tmp_path, results)
    html = path.read_text(encoding="utf-8")
    for tag in ("div", "section", "span", "p", "header", "footer"):
        opens = len(re.findall(rf"<{tag}(\s[^>]*)?>", html))
        closes = len(re.findall(rf"</{tag}>", html))
        assert opens == closes, f"<{tag}> open/close mismatch: {opens} vs {closes}"
    assert html.strip().startswith("<!doctype html>")
    assert html.strip().endswith("</html>")


def test_weekend_edition_and_next_monday_note(tmp_path):
    results = _all_ok_results()
    sunday = datetime(2026, 9, 6, 7, 0, tzinfo=JST)
    path, _ = _generate(tmp_path, results, now=sunday)
    html = path.read_text(encoding="utf-8")
    assert "週末版" in html
    assert "次回09/07(月)" in html


# --- STEP5: 日本市場3指標(参考情報・スコア対象外) ---------------------------

def test_japan_reference_indicators_values_present(tmp_path):
    results = _all_ok_results()
    path, _ = _generate(tmp_path, results)
    html = path.read_text(encoding="utf-8")
    assert "65,820.00" in html and "1.77%" in html   # 日経225先物
    assert "427.50" in html and "0.07%" in html       # TOPIX連動ETF
    assert "2.966%" in html and "4.0bp" in html        # 日本10年債


def test_topix_is_labeled_as_etf_proxy_not_the_index_itself(tmp_path):
    results = _all_ok_results()
    path, _ = _generate(tmp_path, results)
    html = path.read_text(encoding="utf-8")
    assert "TOPIX連動ETF（1306.T）" in html


def test_japan_reference_indicators_marked_as_excluded_from_score(tmp_path):
    results = _all_ok_results()
    path, _ = _generate(tmp_path, results)
    html = path.read_text(encoding="utf-8")
    assert html.count("総合スコアには含まれません") >= 3


def test_japan_indicator_failure_does_not_change_overseas_score(tmp_path):
    results = _all_ok_results()
    results["japan"]["nikkei_futures"] = MetricResult(
        key="nikkei_futures", label="日経225先物", status="ERROR", error="CME snapshot missing"
    )
    path, score_out = _generate(tmp_path, results)
    html = path.read_text(encoding="utf-8")
    # 海外5指標は全件取得できているので、日本側の欠損に関わらずスコアは5指標分のまま
    assert score_out["available"] == 5
    assert score_out["total"] == 5
    assert score_out["provisional"] is False
    assert "CME snapshot missing" in html


# --- STEP6: 今週の重要イベント ------------------------------------------

def _events_out_with(now: datetime, events: list[EventRecord], errors: list[str] | None = None) -> dict:
    return {
        "events": events,
        "errors": errors or [],
        "window_start": now.date().isoformat(),
        "window_end": (now.date() + timedelta(days=7)).isoformat(),
    }


def test_events_appear_in_html(tmp_path):
    now = datetime(2026, 9, 4, 7, 0, tzinfo=JST)
    events = [
        EventRecord(
            date="2026-09-08", time="08:50", country="JP", event="GDP 2次速報（4-6月期）",
            importance="high", source="内閣府", source_url="https://example.com",
        ),
        EventRecord(
            date="2026-09-05", time="21:30", country="US", event="米CPI",
            importance="high", source="FRED", source_url="https://fred.stlouisfed.org/release?rid=10",
        ),
    ]
    results = _all_ok_results()
    path, _ = _generate(tmp_path, results, now=now, events_out=_events_out_with(now, events))
    html = path.read_text(encoding="utf-8")
    assert "GDP 2次速報（4-6月期）" in html
    assert "米CPI" in html
    assert "09/08" in html and "09/05" in html
    assert "08:50" in html and "21:30" in html


def test_no_events_shows_explicit_no_fabrication_message_not_fake_event(tmp_path):
    now = datetime(2026, 9, 4, 7, 0, tzinfo=JST)
    results = _all_ok_results()
    path, _ = _generate(tmp_path, results, now=now, events_out=_events_out_with(now, []))
    html = path.read_text(encoding="utf-8")
    assert "重要イベントはありません" in html
    assert "架空のイベントは表示していません" in html


def test_events_fetch_failure_shows_explicit_message_not_fabricated(tmp_path):
    now = datetime(2026, 9, 4, 7, 0, tzinfo=JST)
    results = _all_ok_results()
    events_out = _events_out_with(now, [], errors=["FRED_API_KEY not set: ..."])
    path, _ = _generate(tmp_path, results, now=now, events_out=events_out)
    html = path.read_text(encoding="utf-8")
    assert "イベントデータ取得不可" in html
    assert "FRED_API_KEY not set" in html


def test_events_do_not_affect_overseas_score(tmp_path):
    now = datetime(2026, 9, 4, 7, 0, tzinfo=JST)
    results = _all_ok_results()
    events = [
        EventRecord(date="2026-09-08", time=None, country="JP", event="テストイベント",
                    importance="high", source="test")
    ]
    _, score_out_with_events = _generate(tmp_path, results, now=now, events_out=_events_out_with(now, events))
    _, score_out_without_events = _generate(tmp_path, results, now=now, events_out=_events_out_with(now, []))
    assert score_out_with_events == score_out_without_events


# --- STEP8-A: 注目ニュース ---------------------------------------------------

def _news_item(category, title, hours_ago, now, url=None, source="テスト通信"):
    published = now - timedelta(hours=hours_ago)
    return NewsItem(
        title=title,
        url=url or f"https://example.com/{category}-{hours_ago}",
        published_at=published.isoformat(),
        source=source,
        category=category,
        matched_keyword="test",
    )


def test_news_appears_grouped_by_category_with_relative_time(tmp_path):
    now = datetime(2026, 9, 4, 7, 0, tzinfo=JST)
    news = [
        _news_item("nvidia", "NVIDIAが新製品発表", 2, now),
        _news_item("boj", "日銀が金融政策を維持", 5, now),
    ]
    news_out = {"news": news, "errors": [], "fetched_at": now.isoformat()}
    path, _ = _generate(tmp_path, _all_ok_results(), now=now, news_out=news_out)
    html = path.read_text(encoding="utf-8")
    assert "注目ニュース" in html
    assert "NVIDIA関連" in html and "日銀" in html
    assert "NVIDIAが新製品発表" in html
    assert "2時間前" in html
    assert "5時間前" in html
    assert "テスト通信" in html


def test_news_caps_at_three_per_category_and_ten_total(tmp_path):
    now = datetime(2026, 9, 4, 7, 0, tzinfo=JST)
    news = [_news_item("nvidia", f"NVIDIAニュース{i}", i, now) for i in range(1, 6)]  # 5件中3件だけ採用されるはず
    news_out = {"news": news, "errors": [], "fetched_at": now.isoformat()}
    path, _ = _generate(tmp_path, _all_ok_results(), now=now, news_out=news_out)
    html = path.read_text(encoding="utf-8")
    assert html.count("NVIDIAニュース") == 3
    # 新しい順(1,2,3時間前)が残り、古い(4,5時間前)は落ちるはず
    assert "NVIDIAニュース1" in html
    assert "NVIDIAニュース3" in html
    assert "NVIDIAニュース4" not in html
    assert "NVIDIAニュース5" not in html


def test_news_excludes_categories_not_in_priority_list(tmp_path):
    now = datetime(2026, 9, 4, 7, 0, tzinfo=JST)
    news = [
        _news_item("us_market", "米金融政策の話題", 1, now),
        _news_item("other", "その他の話題", 1, now),
        _news_item("nvidia", "NVIDIAの話題", 1, now),
    ]
    news_out = {"news": news, "errors": [], "fetched_at": now.isoformat()}
    path, _ = _generate(tmp_path, _all_ok_results(), now=now, news_out=news_out)
    html = path.read_text(encoding="utf-8")
    assert "NVIDIAの話題" in html
    assert "米金融政策の話題" not in html
    assert "その他の話題" not in html


def test_news_zero_results_shows_explicit_message_not_fabricated(tmp_path):
    now = datetime(2026, 9, 4, 7, 0, tzinfo=JST)
    path, _ = _generate(tmp_path, _all_ok_results(), now=now, news_out=_empty_news_out())
    html = path.read_text(encoding="utf-8")
    assert "直近24時間以内に該当する重要ニュースはありません" in html


def test_news_fetch_failure_shows_explicit_message(tmp_path):
    now = datetime(2026, 9, 4, 7, 0, tzinfo=JST)
    news_out = {"news": [], "errors": ["google_news_rss('AI'): timeout"], "fetched_at": now.isoformat()}
    path, _ = _generate(tmp_path, _all_ok_results(), now=now, news_out=news_out)
    html = path.read_text(encoding="utf-8")
    assert "ニュースデータ取得不可" in html
    assert "timeout" in html


def test_news_title_and_source_are_html_escaped(tmp_path):
    now = datetime(2026, 9, 4, 7, 0, tzinfo=JST)
    news = [
        _news_item(
            "nvidia", 'NVIDIA<script>alert("x")</script> & "引用" テスト', 1, now,
            source='テスト<b>通信</b> & Co.',
        )
    ]
    news_out = {"news": news, "errors": [], "fetched_at": now.isoformat()}
    path, _ = _generate(tmp_path, _all_ok_results(), now=now, news_out=news_out)
    html = path.read_text(encoding="utf-8")
    assert '<script>alert("x")</script>' not in html  # ニュースタイトル由来の生スクリプトタグが混入していない
    assert "&lt;script&gt;alert" in html  # エスケープされた形で表示されている
    assert "テスト&lt;b&gt;通信&lt;/b&gt; &amp; Co." in html  # ソース名もエスケープされている


def test_news_does_not_affect_overseas_score(tmp_path):
    now = datetime(2026, 9, 4, 7, 0, tzinfo=JST)
    results = _all_ok_results()
    news = [_news_item("nvidia", "何かのニュース", 1, now)]
    news_out = {"news": news, "errors": [], "fetched_at": now.isoformat()}
    _, score_with_news = _generate(tmp_path, results, now=now, news_out=news_out)
    _, score_without_news = _generate(tmp_path, results, now=now, news_out=_empty_news_out())
    assert score_with_news == score_without_news

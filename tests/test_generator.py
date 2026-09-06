"""STEP4: HTML生成のテスト。ネットワークアクセスは一切行わない。"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from analysis.score import compute_score
from data.market import MetricResult
from generator.html import generate_report

SETTINGS = {"score_threshold_percent": 0.2, "bond_yield_threshold_bp": 3}
JST = ZoneInfo("Asia/Tokyo")


def _ok(key, label, *, change_percent=None, change_bp=None, value=100.0, timestamp="2026-09-04"):
    return MetricResult(
        key=key,
        label=label,
        status="OK",
        value=value,
        previous_value=value - (change_percent or 0),
        change=change_percent or change_bp or 0,
        change_percent=change_percent,
        change_bp=change_bp,
        source=f"test:{key}",
        timestamp=timestamp,
    )


def _all_ok_results():
    return {
        "nasdaq": _ok("nasdaq", "Nasdaq総合", value=26506.99, change_percent=-0.29),
        "sox": _ok("sox", "SOX", value=11735.26, change_percent=3.38),
        "us10y": _ok("us10y", "米10年債利回り", value=4.784, change_bp=2.2),
        "usdjpy": _ok("usdjpy", "ドル円", value=155.78, change_percent=-1.82),
        "kospi": _ok("kospi", "KOSPI", value=6687.21, change_percent=1.64),
    }


def _generate(tmp_path, results, now=None):
    now = now or datetime(2026, 9, 4, 7, 0, tzinfo=JST)  # 平日固定（テストの再現性のため）
    score_out = compute_score(results, SETTINGS)
    path = generate_report(results, score_out, SETTINGS, output_dir=tmp_path, now=now)
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
    results["usdjpy"] = MetricResult(
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
    # S&P500/NYダウ/日本側4指標はSTEP4では未実装であり、でっち上げ数値を入れない
    assert html.count("未実装") >= 6


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

"""STEP3: スコア計算のテスト。ネットワークアクセスは一切行わない。"""

from __future__ import annotations

import pytest

from analysis.score import classify_by_threshold, classify_total, compute_score
from data.market import MetricResult

SETTINGS = {"score_threshold_percent": 0.2, "bond_yield_threshold_bp": 3}


# --- ±0.2%ルール（Nasdaq/SOX/USD-JPY/KOSPI 共通） -----------------------

@pytest.mark.parametrize(
    "change_percent,expected",
    [
        (0.3, 1),
        (0.2, 1),
        (0.19, 0),
        (0.0, 0),
        (-0.19, 0),
        (-0.2, -1),
        (-0.3, -1),
    ],
)
def test_classify_by_threshold_percent(change_percent, expected):
    assert classify_by_threshold(change_percent, 0.2, invert=False) == expected


# --- 米10年債（bp閾値・金利は逆方向） -----------------------------------

@pytest.mark.parametrize(
    "change_bp,expected",
    [
        (-4, 1),
        (-3, 1),
        (-2, 0),
        (0, 0),
        (2, 0),
        (3, -1),
        (4, -1),
    ],
)
def test_classify_by_threshold_bond_bp(change_bp, expected):
    assert classify_by_threshold(change_bp, 3, invert=True) == expected


# --- 総合判定ラベル -------------------------------------------------------

@pytest.mark.parametrize(
    "score,expected",
    [
        (5, "追い風"),
        (3, "追い風"),
        (2, "中立・やや方向感なし"),
        (0, "中立・やや方向感なし"),
        (-2, "中立・やや方向感なし"),
        (-3, "逆風"),
        (-5, "逆風"),
    ],
)
def test_classify_total(score, expected):
    assert classify_total(score) == expected


# --- compute_score: 5指標そろっているケース -------------------------------

def _ok_metric(label: str, *, change_percent=None, change_bp=None) -> MetricResult:
    return MetricResult(
        key=label,
        label=label,
        status="OK",
        value=100.0,
        previous_value=99.0,
        change=1.0,
        change_percent=change_percent,
        change_bp=change_bp,
        source="test",
        timestamp="2026-09-04",
    )


def test_compute_score_all_available():
    results = {
        "nasdaq": _ok_metric("Nasdaq", change_percent=0.3),   # +1
        "sox": _ok_metric("SOX", change_percent=1.5),         # +1
        "us10y": _ok_metric("US10Y", change_bp=-4),           # +1 (低下)
        "usdjpy": _ok_metric("USD/JPY", change_percent=-0.1), # 0
        "kospi": _ok_metric("KOSPI", change_percent=-0.5),    # -1
    }
    out = compute_score(results, SETTINGS)
    assert out["available"] == 5
    assert out["total"] == 5
    assert out["provisional"] is False
    assert out["score"] == 1 + 1 + 1 + 0 - 1  # == 2
    assert out["verdict"] == "中立・やや方向感なし"
    assert out["metrics"]["nasdaq"]["score"] == 1
    assert out["metrics"]["us10y"]["score"] == 1


# --- compute_score: 欠損があるケース --------------------------------------

def test_compute_score_missing_metric_excluded_not_zero():
    results = {
        "nasdaq": _ok_metric("Nasdaq", change_percent=0.3),   # +1
        "sox": _ok_metric("SOX", change_percent=0.5),         # +1
        "us10y": _ok_metric("US10Y", change_bp=-4),           # +1
        "usdjpy": MetricResult(
            key="usdjpy", label="USD/JPY", status="ERROR", error="fetch failed"
        ),
        "kospi": _ok_metric("KOSPI", change_percent=0.5),     # +1
    }
    out = compute_score(results, SETTINGS)
    assert out["available"] == 4
    assert out["total"] == 5
    assert out["provisional"] is True
    assert out["score"] == 4  # 欠損分を0として足しているのではなく、単に含めていない
    assert out["metrics"]["usdjpy"]["status"] == "unavailable"
    assert out["metrics"]["usdjpy"]["score"] is None
    assert "4/5指標" in out["verdict_label"]


def test_compute_score_missing_key_entirely():
    """market.fetch_all()の結果に指標そのものが無いケース(想定外の設定ミス等)でも落ちない。"""
    results = {
        "nasdaq": _ok_metric("Nasdaq", change_percent=0.3),
        "sox": _ok_metric("SOX", change_percent=0.5),
        "us10y": _ok_metric("US10Y", change_bp=-4),
        "kospi": _ok_metric("KOSPI", change_percent=0.5),
        # usdjpy が丸ごと欠落
    }
    out = compute_score(results, SETTINGS)
    assert out["available"] == 4
    assert out["metrics"]["usdjpy"]["status"] == "unavailable"

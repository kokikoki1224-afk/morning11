"""STEP3: USD/JPYがNY17:00スナップショット方式で計算されていることの確認。

ネットワークアクセスはせず、data.market内の取得関数をモックして
「usdjpyの設定では _yfinance_ny17_snapshot が呼ばれ、
日足の _yfinance_last_two_closes は呼ばれない」ことだけを検証する。
"""

from __future__ import annotations

from unittest.mock import patch

from data import market


def test_usdjpy_config_uses_ny17_provider_not_daily():
    settings = market.load_settings()
    usdjpy_cfg = settings["sources"]["usdjpy"]
    assert usdjpy_cfg["primary"]["provider"] == "yfinance_ny17"
    assert usdjpy_cfg["primary"]["symbol"] == "JPY=X"


def test_usdjpy_fetch_calls_ny17_snapshot_not_daily_close():
    settings = market.load_settings()
    usdjpy_cfg = settings["sources"]["usdjpy"]

    with (
        patch.object(
            market, "_yfinance_ny17_snapshot", return_value=(155.78, 158.67, "2026-09-03")
        ) as mock_ny17,
        patch.object(market, "_yfinance_last_two_closes") as mock_daily,
    ):
        result = market.fetch_metric("usdjpy", usdjpy_cfg, is_yield=False)

    mock_ny17.assert_called_once_with("JPY=X")
    mock_daily.assert_not_called()
    assert result.status == "OK"
    assert result.source == "yfinance_ny17:JPY=X"
    assert result.value == 155.78
    assert result.previous_value == 158.67
    assert result.change_percent is not None


def test_usdjpy_fetch_failure_reports_unavailable_not_guessed_value():
    settings = market.load_settings()
    usdjpy_cfg = settings["sources"]["usdjpy"]

    with patch.object(
        market, "_yfinance_ny17_snapshot", side_effect=ValueError("no snapshots")
    ):
        result = market.fetch_metric("usdjpy", usdjpy_cfg, is_yield=False)

    assert result.status == "ERROR"
    assert result.value is None
    assert result.change_percent is None

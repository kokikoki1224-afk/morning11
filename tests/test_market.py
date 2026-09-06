"""USD/JPY・日経225先物がスナップショット方式(日足を使わない)で計算されていること、
および財務省CSVのパースが正しいことの確認。ネットワークアクセスは一切行わない。
"""

from __future__ import annotations

from unittest.mock import patch

from data import market


def test_settings_has_overseas_and_japan_groups_separated():
    settings = market.load_settings()
    assert set(settings["overseas"].keys()) == {"nasdaq", "sox", "us10y", "usdjpy", "kospi"}
    assert set(settings["japan"].keys()) == {"nikkei_futures", "topix_etf", "japan10y"}


# --- USD/JPY: NY17:00スナップショット ------------------------------------

def test_usdjpy_config_uses_snapshot_provider_not_daily():
    settings = market.load_settings()
    cfg = settings["overseas"]["usdjpy"]["primary"]
    assert cfg["provider"] == "yfinance_snapshot"
    assert cfg["symbol"] == "JPY=X"
    assert cfg["tz"] == "America/New_York"
    assert cfg["hour"] == 17
    assert cfg["field"] == "Open"


def test_usdjpy_fetch_calls_hour_snapshot_not_daily_close():
    settings = market.load_settings()
    usdjpy_cfg = settings["overseas"]["usdjpy"]

    with (
        patch.object(
            market, "_yfinance_hour_snapshot", return_value=(155.78, 158.67, "2026-09-03")
        ) as mock_snap,
        patch.object(market, "_yfinance_last_two_closes") as mock_daily,
    ):
        result = market.fetch_metric("usdjpy", usdjpy_cfg, is_yield=False)

    mock_snap.assert_called_once_with("JPY=X", "America/New_York", 17, "Open")
    mock_daily.assert_not_called()
    assert result.status == "OK"
    assert result.source.startswith("yfinance_snapshot:JPY=X")
    assert result.value == 155.78
    assert result.previous_value == 158.67
    assert result.change_percent is not None


def test_usdjpy_fetch_failure_reports_unavailable_not_guessed_value():
    settings = market.load_settings()
    usdjpy_cfg = settings["overseas"]["usdjpy"]

    with patch.object(
        market, "_yfinance_hour_snapshot", side_effect=ValueError("no snapshots")
    ):
        result = market.fetch_metric("usdjpy", usdjpy_cfg, is_yield=False)

    assert result.status == "ERROR"
    assert result.value is None
    assert result.change_percent is None


# --- 日経225先物: JST05:00(CME日次休止直前)スナップショット --------------

def test_nikkei_futures_config_uses_snapshot_provider_not_daily():
    settings = market.load_settings()
    cfg = settings["japan"]["nikkei_futures"]["primary"]
    assert cfg["provider"] == "yfinance_snapshot"
    assert cfg["symbol"] == "NKD=F"
    assert cfg["tz"] == "Asia/Tokyo"
    assert cfg["hour"] == 5
    assert cfg["field"] == "Close"


def test_nikkei_futures_fetch_calls_hour_snapshot_not_daily_close():
    settings = market.load_settings()
    cfg = settings["japan"]["nikkei_futures"]

    with (
        patch.object(
            market, "_yfinance_hour_snapshot", return_value=(65820.0, 64675.0, "2026-09-05")
        ) as mock_snap,
        patch.object(market, "_yfinance_last_two_closes") as mock_daily,
    ):
        result = market.fetch_metric("nikkei_futures", cfg, is_yield=False)

    mock_snap.assert_called_once_with("NKD=F", "Asia/Tokyo", 5, "Close")
    mock_daily.assert_not_called()
    assert result.status == "OK"
    assert result.value == 65820.0


# --- TOPIX連動ETF(1306.T): 単一セッションなので通常の日足でよい ------------

def test_topix_etf_config_uses_plain_daily_close():
    settings = market.load_settings()
    cfg = settings["japan"]["topix_etf"]["primary"]
    assert cfg["provider"] == "yfinance"
    assert cfg["symbol"] == "1306.T"


# --- 日本10年債: 財務省公式CSV --------------------------------------------

def test_japan10y_config_uses_mof_official_source():
    settings = market.load_settings()
    cfg = settings["japan"]["japan10y"]["primary"]
    assert cfg["provider"] == "mof_jgb10y"


MOF_SAMPLE_CSV = (
    "国債金利情報 (令和8年9月),,,,,,,,,,,,,,,(単位 : %)\n"
    "基準日,1年,2年,3年,4年,5年,6年,7年,8年,9年,10年,15年,20年,25年,30年,40年\n"
    "R8.9.1,1.527,1.802,1.952,2.14,2.28,2.411,2.559,2.718,2.848,2.987,3.544,3.859,4.143,4.131,4.145\n"
    "R8.9.2,1.56,1.854,2.009,2.199,2.332,2.45,2.585,2.743,2.874,3.006,3.554,3.864,4.141,4.122,4.134\n"
    "R8.9.3,1.563,1.85,1.994,2.173,2.303,2.426,2.547,2.704,2.832,2.966,3.506,3.806,4.079,4.052,4.063\n"
    ",,,,,,,,,,,,,,,\n"
    "※注意文,,,,,,,,,,,,,,,\n"
)


def test_parse_mof_csv_text_extracts_10y_column_in_order():
    rows = market._parse_mof_csv_text(MOF_SAMPLE_CSV)
    assert rows == [
        ("2026-09-01", 2.987),
        ("2026-09-02", 3.006),
        ("2026-09-03", 2.966),
    ]


def test_era_to_iso_date_reiwa():
    assert market._era_to_iso_date("R8.9.3") == "2026-09-03"


def test_mof_jgb10y_last_two_uses_parsed_csv_and_computes_bp_change():
    settings = market.load_settings()
    cfg = settings["japan"]["japan10y"]

    with patch.object(market, "_fetch_mof_csv_text", return_value=MOF_SAMPLE_CSV):
        result = market.fetch_metric("japan10y", cfg, is_yield=True)

    assert result.status == "OK"
    assert result.value == 2.966
    assert result.previous_value == 3.006
    # 3.006% -> 2.966% は -4.0bp
    assert result.change_bp == -4.0
    assert result.timestamp == "2026-09-03"


def test_mof_jgb10y_falls_back_to_all_csv_when_recent_csv_insufficient():
    single_row_csv = (
        "header,,,,,,,,,,,,,,,\n"
        "基準日,1年,2年,3年,4年,5年,6年,7年,8年,9年,10年,15年,20年,25年,30年,40年\n"
        "R8.9.3,1.563,1.85,1.994,2.173,2.303,2.426,2.547,2.704,2.832,2.966,3.506,3.806,4.079,4.052,4.063\n"
    )

    def fake_fetch(url):
        if url == market.MOF_RECENT_CSV_URL:
            return single_row_csv
        return MOF_SAMPLE_CSV

    with patch.object(market, "_fetch_mof_csv_text", side_effect=fake_fetch):
        latest, prev, date = market._mof_jgb10y_last_two()

    assert (latest, prev, date) == (2.966, 3.006, "2026-09-03")

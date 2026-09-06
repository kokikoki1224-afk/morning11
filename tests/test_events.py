"""STEP6: 経済イベントカレンダーのテスト。ネットワークアクセスは一切行わない。"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from data import events

JST = ZoneInfo("Asia/Tokyo")

SETTINGS = {
    "events": {
        "days_ahead": 7,
        "importance_filter": ["high"],
        "fred_releases": {
            "米CPI": {"release_id": 10, "country": "US", "importance": "high", "release_time_et": "08:30"},
            "米小売売上高": {"release_id": 9, "country": "US", "importance": "medium", "release_time_et": "08:30"},
        },
    }
}


# --- JST変換 ---------------------------------------------------------------

def test_to_jst_same_day_for_morning_et_release():
    # 8:30 ET (EDT, UTC-4) は同日21:30 JSTになる（日付をまたがない）
    d, t = events._to_jst("2026-09-05", "08:30", "America/New_York")
    assert (d, t) == ("2026-09-05", "21:30")


def test_to_jst_crosses_midnight_for_afternoon_et_release():
    # 14:00 ET (EDT) は翌日03:00 JSTになる（日付をまたぐ）
    d, t = events._to_jst("2026-09-16", "14:00", "America/New_York")
    assert (d, t) == ("2026-09-17", "03:00")


def test_to_jst_passthrough_when_tz_is_none():
    # 日本側の静的データはすでにJSTなので変換しない
    d, t = events._to_jst("2026-09-18", "11:30", None)
    assert (d, t) == ("2026-09-18", "11:30")


def test_to_jst_returns_date_only_when_time_missing():
    d, t = events._to_jst("2026-09-15", None, None)
    assert (d, t) == ("2026-09-15", None)


# --- FRED経由の米国主要指標イベント -----------------------------------------

def _fake_fred_response(dates: list[str]) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"release_dates": [{"date": d} for d in dates]}
    return resp


def test_fetch_fred_events_converts_to_jst_and_filters_window(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "dummy")
    window_start = datetime(2026, 9, 6, tzinfo=JST).date()
    window_end = datetime(2026, 9, 13, tzinfo=JST).date()

    with patch.object(
        events.requests, "get",
        return_value=_fake_fred_response(["2026-08-13", "2026-09-11", "2026-10-13"]),
    ):
        evs, errors = events.fetch_fred_events(SETTINGS["events"], window_start, window_end)

    assert errors == []
    dates = {e.date for e in evs}
    assert "2026-09-11" in dates  # 窓内
    assert "2026-08-13" not in dates  # 過去（窓外）
    assert "2026-10-13" not in dates  # 先すぎる（窓外）
    hit = next(e for e in evs if e.date == "2026-09-11")
    assert hit.time == "21:30"  # 08:30 ET -> 21:30 JST
    assert hit.country == "US"
    assert hit.source == "FRED"


def test_fetch_fred_events_missing_api_key_reports_error_not_fabricated():
    window_start = datetime(2026, 9, 6, tzinfo=JST).date()
    window_end = datetime(2026, 9, 13, tzinfo=JST).date()
    with patch.dict("os.environ", {}, clear=True):
        evs, errors = events.fetch_fred_events(SETTINGS["events"], window_start, window_end)
    assert evs == []
    assert any("FRED_API_KEY" in e for e in errors)


def test_fetch_fred_events_per_release_failure_does_not_abort_others(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "dummy")
    window_start = datetime(2026, 9, 6, tzinfo=JST).date()
    window_end = datetime(2026, 9, 13, tzinfo=JST).date()

    def fake_get(url, params=None, timeout=None):
        if params["release_id"] == 10:
            raise ConnectionError("network down")
        return _fake_fred_response(["2026-09-11"])

    with patch.object(events.requests, "get", side_effect=fake_get):
        evs, errors = events.fetch_fred_events(SETTINGS["events"], window_start, window_end)

    assert len(evs) == 1  # release_id=9(小売売上高)側は成功
    assert any("network down" in e for e in errors)


# --- 静的カレンダー(FOMC/日銀等) ---------------------------------------------

def test_load_static_calendar_filters_window_and_converts_jst(tmp_path, monkeypatch):
    calendar = [
        {"date": "2026-09-16", "time": "14:00", "tz": "America/New_York",
         "country": "US", "event": "FOMC声明発表", "importance": "high",
         "source": "FRB", "source_url": "https://example.com"},
        {"date": "2026-01-01", "time": None, "tz": None,
         "country": "JP", "event": "過去のイベント", "importance": "high", "source": "test"},
    ]
    calendar_path = tmp_path / "events_calendar.json"
    calendar_path.write_text(json.dumps(calendar), encoding="utf-8")
    monkeypatch.setattr(events, "STATIC_CALENDAR_PATH", calendar_path)

    window_start = datetime(2026, 9, 6, tzinfo=JST).date()
    window_end = datetime(2026, 9, 20, tzinfo=JST).date()
    evs, errors = events.load_static_calendar(window_start, window_end)

    assert errors == []
    assert len(evs) == 1  # 過去のイベントは窓外なので除外
    assert evs[0].date == "2026-09-17"  # 14:00 ET -> 翌日03:00 JSTに日付が繰り上がる
    assert evs[0].time == "03:00"


def test_load_static_calendar_missing_file_reports_error_not_fabricated(tmp_path, monkeypatch):
    monkeypatch.setattr(events, "STATIC_CALENDAR_PATH", tmp_path / "does_not_exist.json")
    window_start = datetime(2026, 9, 6, tzinfo=JST).date()
    window_end = datetime(2026, 9, 13, tzinfo=JST).date()
    evs, errors = events.load_static_calendar(window_start, window_end)
    assert evs == []
    assert len(errors) == 1


# --- fetch_events: 統合・重要度フィルタ・0件表示 -----------------------------

def test_fetch_events_applies_importance_filter(monkeypatch, tmp_path):
    monkeypatch.setenv("FRED_API_KEY", "dummy")
    calendar_path = tmp_path / "events_calendar.json"
    calendar_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(events, "STATIC_CALENDAR_PATH", calendar_path)

    now = datetime(2026, 9, 6, 7, 0, tzinfo=JST)
    with patch.object(events.requests, "get", return_value=_fake_fred_response(["2026-09-08"])):
        out = events.fetch_events(SETTINGS, now=now)

    # 米小売売上高はimportance=mediumなので、importance_filter=["high"]で除外される
    assert all(e.importance == "high" for e in out["events"])
    assert all(e.event != "米小売売上高" for e in out["events"])


def test_fetch_events_within_7day_window_only(monkeypatch, tmp_path):
    monkeypatch.setenv("FRED_API_KEY", "dummy")
    calendar_path = tmp_path / "events_calendar.json"
    calendar_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(events, "STATIC_CALENDAR_PATH", calendar_path)

    now = datetime(2026, 9, 6, 7, 0, tzinfo=JST)
    with patch.object(
        events.requests, "get",
        return_value=_fake_fred_response(["2026-09-08", "2026-09-20"]),
    ):
        out = events.fetch_events(SETTINGS, now=now)

    dates = {e.date for e in out["events"]}
    assert "2026-09-08" in dates
    assert "2026-09-20" not in dates  # 7日超は対象外
    assert out["window_start"] == "2026-09-06"
    assert out["window_end"] == "2026-09-13"


def test_fetch_events_zero_events_returns_empty_list_not_error(monkeypatch, tmp_path):
    monkeypatch.setenv("FRED_API_KEY", "dummy")
    calendar_path = tmp_path / "events_calendar.json"
    calendar_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(events, "STATIC_CALENDAR_PATH", calendar_path)

    now = datetime(2026, 9, 6, 7, 0, tzinfo=JST)
    with patch.object(events.requests, "get", return_value=_fake_fred_response([])):
        out = events.fetch_events(SETTINGS, now=now)

    assert out["events"] == []
    assert out["errors"] == []

"""経済イベントカレンダー取得（STEP6）。

設計原則:
- ここでは「イベントの事実データ（日付・時刻・国・イベント名・重要度）」の取得と
  JST変換・期間フィルタだけを行う。「このイベントが日本株にどう影響するか」の解釈は
  一切行わない（後続ステップでAIが担当する）。
- 取得できないイベントは推測で埋めない。FRED側が失敗しても静的カレンダー側は
  独立して動作し、逆も同様（片方の失敗がもう片方を巻き込まない）。
- 米国の主要指標（CPI/雇用統計/GDP/小売売上高/PCE）はFRED公式APIの
  release/dates エンドポイントで実際に自動取得する。
- FOMC・日銀会合・日本GDP等は、STEP6時点で安定した無料APIが見つからなかったため、
  config/events_calendar.json の手動メンテナンス静的データを使用する
  （実在する公式発表予定を転記したものであり、架空のイベントではない。README参照）。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

STATIC_CALENDAR_PATH = Path(__file__).resolve().parent.parent / "config" / "events_calendar.json"
FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"
JST = ZoneInfo("Asia/Tokyo")
REQUEST_TIMEOUT = 15


@dataclass
class EventRecord:
    date: str  # JST基準の日付 "YYYY-MM-DD"
    time: str | None  # JST基準の時刻 "HH:MM"（不明な場合はNone）
    country: str
    event: str
    importance: str  # "high" / "medium" / "low"
    source: str
    source_url: str | None = None
    actual: Any = None
    forecast: Any = None
    previous: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_jst(local_date: str, local_time: str | None, tz_name: str | None) -> tuple[str, str | None]:
    """(現地日付, 現地時刻, タイムゾーン名) を JST の (日付, 時刻) に変換する。

    tz_name が None の場合は「すでにJST」とみなしてそのまま返す（日本側の静的データ用）。
    local_time が None の場合は時刻不明として日付のみ変換する。
    """
    if not local_time:
        return local_date, None
    if not tz_name:
        return local_date, local_time
    naive = datetime.strptime(f"{local_date} {local_time}", "%Y-%m-%d %H:%M")
    aware = naive.replace(tzinfo=ZoneInfo(tz_name))
    jst = aware.astimezone(JST)
    return jst.strftime("%Y-%m-%d"), jst.strftime("%H:%M")


def _fetch_fred_release_dates(release_id: int, api_key: str) -> list[str]:
    params = {
        "release_id": release_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "asc",
        "include_release_dates_with_no_data": "true",
        "limit": 1000,
    }
    resp = requests.get(FRED_RELEASE_DATES_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return [d["date"] for d in resp.json().get("release_dates", [])]


def fetch_fred_events(
    events_settings: dict[str, Any], window_start: date, window_end: date
) -> tuple[list[EventRecord], list[str]]:
    """FRED公式APIから米国主要指標の発表日を取得する。戻り値は (events, errors)。"""
    events: list[EventRecord] = []
    errors: list[str] = []

    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        errors.append("FRED_API_KEY not set: 米国主要指標カレンダーは取得できません")
        return events, errors

    # FRED側の日付フィルタ(realtime_start/end)は「発表日リスト自体の改定履歴」用であり
    # 発表日そのものの絞り込みには使えないため、余裕を持って全件取得しクライアント側で絞る
    fetch_margin = timedelta(days=2)

    for name, cfg in events_settings.get("fred_releases", {}).items():
        try:
            raw_dates = _fetch_fred_release_dates(cfg["release_id"], api_key)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"FRED release '{name}'(release_id={cfg.get('release_id')}): {exc}")
            continue

        release_time_et = cfg.get("release_time_et")
        for raw_date in raw_dates:
            try:
                us_date_obj = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            if not (window_start - fetch_margin <= us_date_obj <= window_end + fetch_margin):
                continue  # 明らかに範囲外の日付はJST変換前に間引く（無駄なオブジェクト生成を避ける）

            jst_date, jst_time = _to_jst(raw_date, release_time_et, "America/New_York")
            jst_date_obj = datetime.strptime(jst_date, "%Y-%m-%d").date()
            if not (window_start <= jst_date_obj <= window_end):
                continue

            events.append(
                EventRecord(
                    date=jst_date,
                    time=jst_time,
                    country=cfg.get("country", "US"),
                    event=name,
                    importance=cfg.get("importance", "medium"),
                    source="FRED",
                    source_url=f"https://fred.stlouisfed.org/release?rid={cfg['release_id']}",
                )
            )

    return events, errors


def load_static_calendar(window_start: date, window_end: date) -> tuple[list[EventRecord], list[str]]:
    """config/events_calendar.json（手動メンテナンスの静的データ）を読み込む。

    FOMC・日銀会合・日本GDP等、STEP6時点で安定した無料APIが見つからなかったイベント用。
    実在する公式発表予定を人手で転記したものであり、推測で埋めたものではない。
    """
    events: list[EventRecord] = []
    errors: list[str] = []
    try:
        with open(STATIC_CALENDAR_PATH, encoding="utf-8") as f:
            raw_items = json.load(f)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"static calendar ({STATIC_CALENDAR_PATH.name}) load failed: {exc}")
        return events, errors

    for item in raw_items:
        try:
            jst_date, jst_time = _to_jst(item["date"], item.get("time"), item.get("tz"))
            jst_date_obj = datetime.strptime(jst_date, "%Y-%m-%d").date()
            if not (window_start <= jst_date_obj <= window_end):
                continue
            events.append(
                EventRecord(
                    date=jst_date,
                    time=jst_time,
                    country=item["country"],
                    event=item["event"],
                    importance=item.get("importance", "medium"),
                    source=item.get("source", "(手動メンテナンス静的データ)"),
                    source_url=item.get("source_url"),
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"static calendar entry invalid ({item.get('event', '?')}): {exc}")

    return events, errors


def fetch_events(settings: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """今週の重要イベントを取得する。総合スコア（analysis/score.py）には一切関与しない。"""
    now = now or datetime.now(JST)
    events_settings = settings.get("events", {})
    days_ahead = events_settings.get("days_ahead", 7)
    importance_filter = events_settings.get("importance_filter") or []

    window_start = now.date()
    window_end = window_start + timedelta(days=days_ahead)

    fred_events, fred_errors = fetch_fred_events(events_settings, window_start, window_end)
    static_events, static_errors = load_static_calendar(window_start, window_end)

    all_events = fred_events + static_events
    if importance_filter:
        all_events = [e for e in all_events if e.importance in importance_filter]
    all_events.sort(key=lambda e: (e.date, e.time or "99:99"))

    return {
        "events": all_events,
        "errors": fred_errors + static_errors,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
    }


if __name__ == "__main__":
    from data.market import load_settings

    out = fetch_events(load_settings())
    print(f"window: {out['window_start']} .. {out['window_end']}")
    for e in out["events"]:
        print(e.to_dict())
    if out["errors"]:
        print("errors:", out["errors"])

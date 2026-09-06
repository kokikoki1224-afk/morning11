"""市場データ取得（STEP2: Nasdaq / SOX / 米10年債利回り / USD/JPY / KOSPI）。

設計原則（プロジェクト計画より）:
- 数値はここで確定値として取得するのみで、スコア判定・解釈は行わない（analysis/側の責務）。
- 取得できなかった指標は前日値や推測で埋めず、status="ERROR" として明示する。
- データソースは config/settings.json で差し替え可能にし、コードにハードコードしない。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.json"
FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
STOOQ_CSV_URL = "https://stooq.com/q/d/l/"
REQUEST_TIMEOUT = 15


@dataclass
class MetricResult:
    key: str
    label: str
    status: str  # "OK" or "ERROR"
    value: float | None = None
    previous_value: float | None = None
    change: float | None = None
    change_percent: float | None = None
    change_bp: float | None = None
    source: str | None = None
    symbol: str | None = None
    timestamp: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def load_settings() -> dict[str, Any]:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _stooq_last_two_closes(symbol: str) -> tuple[float, float, str]:
    """Stooqの日足CSVから直近2本の終値を取り出す。"""
    url = f"{STOOQ_CSV_URL}?s={urllib.parse.quote(symbol)}&i=d"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    text = resp.text.strip()
    if not text or text.startswith("Exceeded") or "No data" in text:
        raise ValueError(f"stooq: no data for symbol '{symbol}'")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        raise ValueError(f"stooq: insufficient rows for symbol '{symbol}'")
    header = lines[0].split(",")
    close_idx = header.index("Close")
    date_idx = header.index("Date")
    last = lines[-1].split(",")
    prev = lines[-2].split(",")
    return float(last[close_idx]), float(prev[close_idx]), last[date_idx]


def _yfinance_last_two_closes(symbol: str) -> tuple[float, float, str]:
    import yfinance as yf

    hist = yf.Ticker(symbol).history(period="10d")
    closes = hist["Close"].dropna()
    if len(closes) < 2:
        raise ValueError(f"yfinance: insufficient history for symbol '{symbol}'")
    last_ts = closes.index[-1]
    return float(closes.iloc[-1]), float(closes.iloc[-2]), str(last_ts.date())


def _yfinance_ny17_snapshot(symbol: str) -> tuple[float, float, str]:
    """24時間取引される銘柄(USD/JPY等)向け。

    yfinanceの日足(interval=1d)は、24時間連続で取引されるFXペアだと
    「Date」ラベルの実際の集計区間があいまいで、日によっては前日比の符号が
    入れ替わるほど値がずれることを実データ検証で確認した(README参照)。
    そのため時間足(interval=1h)を取得し、日本の相場報道が慣行的に使う
    「NY17時(米東部時間)」に最も近いバー(17:00-18:00 ETのOpen=ちょうど17:00の値)
    を各日から取り出して前日比を計算する。
    """
    import yfinance as yf

    hist = yf.Ticker(symbol).history(period="10d", interval="1h")
    if hist.empty:
        raise ValueError(f"yfinance(1h): no data for symbol '{symbol}'")
    hist = hist.tz_convert("America/New_York")
    snap = hist[hist.index.hour == 17]["Open"].dropna()
    if len(snap) < 2:
        raise ValueError(f"yfinance(1h): insufficient NY17:00 snapshots for symbol '{symbol}'")
    last_ts = snap.index[-1]
    return float(snap.iloc[-1]), float(snap.iloc[-2]), str(last_ts.date())


def _fred_last_two_values(series_id: str, api_key: str) -> tuple[float, float, str]:
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 20,
    }
    resp = requests.get(FRED_OBSERVATIONS_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    obs = resp.json().get("observations", [])
    valid = [o for o in obs if o.get("value") not in (None, ".", "")]
    if len(valid) < 2:
        raise ValueError(f"fred: insufficient observations for series '{series_id}'")
    return float(valid[0]["value"]), float(valid[1]["value"]), valid[0]["date"]


def _fetch_from_provider(source_cfg: dict[str, Any]) -> tuple[float, float, str, str]:
    """source_cfg (primary/fallback片方) を使って (latest, previous, date, source_label) を返す。"""
    provider = source_cfg["provider"]
    if provider == "stooq":
        latest, prev, date = _stooq_last_two_closes(source_cfg["symbol"])
        return latest, prev, date, f"stooq:{source_cfg['symbol']}"
    if provider == "yfinance":
        latest, prev, date = _yfinance_last_two_closes(source_cfg["symbol"])
        divide_by = source_cfg.get("divide_by")
        if divide_by:
            latest, prev = latest / divide_by, prev / divide_by
        return latest, prev, date, f"yfinance:{source_cfg['symbol']}"
    if provider == "yfinance_ny17":
        latest, prev, date = _yfinance_ny17_snapshot(source_cfg["symbol"])
        return latest, prev, date, f"yfinance_ny17:{source_cfg['symbol']}"
    if provider == "fred":
        api_key = os.environ.get("FRED_API_KEY")
        if not api_key:
            raise RuntimeError("FRED_API_KEY not set")
        latest, prev, date = _fred_last_two_values(source_cfg["series_id"], api_key)
        return latest, prev, date, f"fred:{source_cfg['series_id']}"
    raise ValueError(f"unknown provider: {provider}")


def fetch_metric(key: str, cfg: dict[str, Any], is_yield: bool = False) -> MetricResult:
    label = cfg["label"]
    attempts = [cfg["primary"]]
    if cfg.get("fallback"):
        attempts.append(cfg["fallback"])

    last_error: str | None = None
    for i, source_cfg in enumerate(attempts):
        try:
            latest, prev, date, source_label = _fetch_from_provider(source_cfg)
            change = latest - prev
            result = MetricResult(
                key=key,
                label=label,
                status="OK",
                value=round(latest, 4),
                previous_value=round(prev, 4),
                change=round(change, 4),
                source=source_label,
                symbol=source_cfg.get("symbol") or source_cfg.get("series_id"),
                timestamp=date,
            )
            if is_yield:
                result.change_bp = round(change * 100, 1)  # %ポイント → bp
            else:
                result.change_percent = round((change / prev) * 100, 3) if prev else None
            return result
        except Exception as exc:  # noqa: BLE001 - フォールバックへ進むため意図的に広く捕捉
            role = "primary" if i == 0 else "fallback"
            msg = f"{key} {role}({source_cfg.get('provider')}): {exc}"
            print(f"[WARN] {msg}", file=sys.stderr)
            last_error = msg
            continue

    return MetricResult(key=key, label=label, status="ERROR", error=last_error)


def fetch_all(settings: dict[str, Any] | None = None) -> dict[str, MetricResult]:
    settings = settings or load_settings()
    sources = settings["sources"]
    results: dict[str, MetricResult] = {}
    for key, cfg in sources.items():
        is_yield = key == "us10y"
        results[key] = fetch_metric(key, cfg, is_yield=is_yield)
    return results


if __name__ == "__main__":
    for key, result in fetch_all().items():
        print(key, result.to_dict())

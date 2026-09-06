"""市場データ取得（STEP2: 海外5指標 / STEP5: 日本市場3指標）。

設計原則（プロジェクト計画より）:
- 数値はここで確定値として取得するのみで、スコア判定・解釈は行わない（analysis/側の責務）。
- 取得できなかった指標は前日値や推測で埋めず、status="ERROR" として明示する。
- データソースは config/settings.json で差し替え可能にし、コードにハードコードしない。

データ構造（STEP5で overseas/japan に分離、混同しない）:
    fetch_all() -> {
        "overseas": {"nasdaq": MetricResult, "sox": ..., "us10y": ..., "usdjpy": ..., "kospi": ...},
        "japan":    {"nikkei_futures": MetricResult, "topix_etf": ..., "japan10y": ...},
    }
日本市場3指標は参考情報であり、総合スコア（analysis/score.py）には一切含めない。
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.json"
FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
MOF_RECENT_CSV_URL = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
MOF_ALL_CSV_URL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"
REQUEST_TIMEOUT = 15

# 米10年債・日本10年債など、bp(ベーシスポイント)で前日比を扱う指標
YIELD_KEYS = {"us10y", "japan10y"}


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


def _yfinance_last_two_closes(symbol: str) -> tuple[float, float, str]:
    import yfinance as yf

    hist = yf.Ticker(symbol).history(period="10d")
    closes = hist["Close"].dropna()
    if len(closes) < 2:
        raise ValueError(f"yfinance: insufficient history for symbol '{symbol}'")
    last_ts = closes.index[-1]
    return float(closes.iloc[-1]), float(closes.iloc[-2]), str(last_ts.date())


def _yfinance_hour_snapshot(symbol: str, tz_name: str, hour: int, field: str) -> tuple[float, float, str]:
    """ほぼ24時間連続で取引される銘柄(USD/JPY・CME日経225先物等)向け。

    日足(interval=1d)は連続取引銘柄だと集計区間があいまいになりやすい
    （USD/JPYで前日比の符号が逆転する不整合を実データ検証で確認済み、README参照）。
    そのため時間足(interval=1h)を取得し、指定タイムゾーンの指定時刻に最も近いバーを
    毎日切り出して前日比を計算する。

    例:
        USD/JPY: tz="America/New_York", hour=17, field="Open"
            → 為替報道が慣行的に使う「NY17時」のちょうどその時刻の値。
        日経225先物(NKD=F): tz="Asia/Tokyo", hour=5, field="Close"
            → CMEはJST 06:00〜07:00頃に日次メンテナンス休止があり、その直前の
              JST05時台バーの終値が「休止前＝大取夜間取引終了に相当」する値として
              最も安定して取得できると実データ検証で確認済み（README参照）。
    """
    import yfinance as yf

    hist = yf.Ticker(symbol).history(period="10d", interval="1h")
    if hist.empty:
        raise ValueError(f"yfinance(1h): no data for symbol '{symbol}'")
    hist = hist.tz_convert(tz_name)
    snap = hist[hist.index.hour == hour][field].dropna()
    if len(snap) < 2:
        raise ValueError(
            f"yfinance(1h): insufficient {tz_name} {hour:02d}:00 snapshots for symbol '{symbol}'"
        )
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


_MOF_ERA_OFFSET = {"S": 1925, "H": 1988, "R": 2018}  # 昭和/平成/令和 → 西暦への加算
_MOF_ROW_RE = re.compile(r"^([SHR])(\d+)\.(\d+)\.(\d+),")


def _era_to_iso_date(token: str) -> str:
    m = re.match(r"^([SHR])(\d+)\.(\d+)\.(\d+)$", token)
    if not m:
        raise ValueError(f"mof: unrecognized date token '{token}'")
    era, era_year, month, day = m.groups()
    year = _MOF_ERA_OFFSET[era] + int(era_year)
    return f"{year:04d}-{int(month):02d}-{int(day):02d}"


def _parse_mof_csv_text(text: str) -> list[tuple[str, float]]:
    """財務省 国債金利情報CSVをパースし、[(ISO日付, 10年債利回り), ...] を古い順に返す。

    列は 基準日,1年,2年,...,9年,10年,15年,... の順（10年は0始まりで10番目、
    先頭の日付列を含めると11列目）。値が空欄/"-"の行はスキップする。
    """
    rows: list[tuple[str, float]] = []
    for line in text.splitlines():
        line = line.strip()
        if not _MOF_ROW_RE.match(line + ","):
            continue
        parts = line.split(",")
        if len(parts) < 11:
            continue
        value_token = parts[10].strip()
        if value_token in ("", "-"):
            continue
        try:
            date_iso = _era_to_iso_date(parts[0].strip())
            value = float(value_token)
        except ValueError:
            continue
        rows.append((date_iso, value))
    return rows


def _fetch_mof_csv_text(url: str) -> str:
    resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.content.decode("cp932", errors="ignore")


def _mof_jgb10y_last_two() -> tuple[float, float, str]:
    rows = _parse_mof_csv_text(_fetch_mof_csv_text(MOF_RECENT_CSV_URL))
    if len(rows) < 2:
        # 月初など当月分CSVの行数が足りない場合は、全期間CSVにフォールバックする
        rows = _parse_mof_csv_text(_fetch_mof_csv_text(MOF_ALL_CSV_URL))
    if len(rows) < 2:
        raise ValueError("mof: insufficient rows for 10y yield")
    (latest_date, latest_value), (_, prev_value) = rows[-1], rows[-2]
    return latest_value, prev_value, latest_date


def _fetch_from_provider(source_cfg: dict[str, Any]) -> tuple[float, float, str, str]:
    """source_cfg (primary/fallback片方) を使って (latest, previous, date, source_label) を返す。"""
    provider = source_cfg["provider"]
    if provider == "yfinance":
        latest, prev, date = _yfinance_last_two_closes(source_cfg["symbol"])
        divide_by = source_cfg.get("divide_by")
        if divide_by:
            latest, prev = latest / divide_by, prev / divide_by
        return latest, prev, date, f"yfinance:{source_cfg['symbol']}"
    if provider == "yfinance_snapshot":
        latest, prev, date = _yfinance_hour_snapshot(
            source_cfg["symbol"], source_cfg["tz"], source_cfg["hour"], source_cfg["field"]
        )
        return latest, prev, date, f"yfinance_snapshot:{source_cfg['symbol']}@{source_cfg['tz']}{source_cfg['hour']:02d}"
    if provider == "fred":
        api_key = os.environ.get("FRED_API_KEY")
        if not api_key:
            raise RuntimeError("FRED_API_KEY not set")
        latest, prev, date = _fred_last_two_values(source_cfg["series_id"], api_key)
        return latest, prev, date, f"fred:{source_cfg['series_id']}"
    if provider == "mof_jgb10y":
        latest, prev, date = _mof_jgb10y_last_two()
        return latest, prev, date, "mof_jgb10y"
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


def _fetch_group(group_settings: dict[str, Any]) -> dict[str, MetricResult]:
    results: dict[str, MetricResult] = {}
    for key, cfg in group_settings.items():
        results[key] = fetch_metric(key, cfg, is_yield=key in YIELD_KEYS)
    return results


def fetch_all(settings: dict[str, Any] | None = None) -> dict[str, dict[str, MetricResult]]:
    """{"overseas": {...5指標...}, "japan": {...3指標...}} を返す。

    overseas(総合スコア対象の5指標)と japan(参考情報の3指標)は明確に分離しており、
    analysis/score.py には overseas 側だけを渡すこと（日本側はスコアに含めない）。
    """
    settings = settings or load_settings()
    return {
        "overseas": _fetch_group(settings["overseas"]),
        "japan": _fetch_group(settings["japan"]),
    }


if __name__ == "__main__":
    for group, group_results in fetch_all().items():
        for key, result in group_results.items():
            print(group, key, result.to_dict())

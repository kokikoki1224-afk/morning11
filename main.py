"""日本株モーニング11 自動化 - エントリポイント。

現時点のスコープ（STEP2+STEP3+STEP4+STEP5+STEP6）:
- 海外5指標（Nasdaq/SOX/米10年債/USD-JPY/KOSPI）の自動取得＋±0.2%ルール・bp閾値によるスコア計算
- 日本市場3指標（日経225先物/TOPIX連動ETF/日本10年債）の自動取得（総合スコアには含めない参考情報）
- 今週の重要イベント（米国主要指標はFRED、FOMC/日銀等は静的カレンダー）の取得・JST変換・期間フィルタ
- 既存デザインのHTMLへの埋め込み
AI分析・ニュース取得・GitHub Actions/Pagesは未実装（後続ステップ）。

使い方:
    python main.py --test              # 5指標の取得＋スコア計算を行い、結果をログ出力
    python main.py --generate          # 上記に加え output/ にHTMLを生成する
    python main.py --test --date ...   # 過去日付での再実行は未実装（後続ステップで対応）
"""

from __future__ import annotations

import argparse
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from analysis.score import compute_score
from data import events as events_module
from data import market
from generator.html import generate_report


def log_result(result: market.MetricResult) -> None:
    if result.status == "OK":
        if result.change_bp is not None:
            change_str = f"{result.value} ({result.change:+.3f}pt / {result.change_bp:+.1f}bp)"
        else:
            change_str = f"{result.value} ({result.change:+.2f} / {result.change_percent:+.2f}%)"
        print(f"[OK] {result.label}: {change_str}  source={result.source} date={result.timestamp}")
    else:
        print(f"[ERROR] {result.label} data unavailable ({result.error})", file=sys.stderr)


def fetch_and_score() -> tuple[dict[str, dict[str, market.MetricResult]], dict, dict]:
    settings = market.load_settings()
    results = market.fetch_all(settings)  # {"overseas": {...5指標}, "japan": {...3指標(参考)}}

    print("[海外5指標（総合スコア対象）]")
    ok_count = 0
    for key in ("nasdaq", "sox", "us10y", "usdjpy", "kospi"):
        result = results["overseas"].get(key)
        if result is None:
            print(f"[ERROR] {key}: 設定に見つかりません", file=sys.stderr)
            continue
        log_result(result)
        if result.status == "OK":
            ok_count += 1
    print(f"--- {ok_count}/{len(results['overseas'])} 指標を取得できました ---")

    print("[日本市場3指標（参考情報・スコア対象外）]")
    for key in ("nikkei_futures", "topix_etf", "japan10y"):
        result = results["japan"].get(key)
        if result is None:
            print(f"[ERROR] {key}: 設定に見つかりません", file=sys.stderr)
            continue
        log_result(result)

    score_out = compute_score(results["overseas"], settings)
    print(f"[OK] Score: {score_out['verdict_label']}")
    for key, m in score_out["metrics"].items():
        if m["status"] == "ok":
            print(f"  {key}: score={m['score']:+d}")
        else:
            print(f"  {key}: unavailable ({m['error']})")

    return results, score_out, settings


def fetch_events(settings: dict) -> dict:
    print("[今週の重要イベント（参考情報・スコア対象外）]")
    events_out = events_module.fetch_events(settings)
    print(f"--- window: {events_out['window_start']} .. {events_out['window_end']} ---")
    for e in events_out["events"]:
        print(f"[OK] {e.date} {e.time or '(時刻未定)'} {e.country} {e.event} importance={e.importance}")
    for err in events_out["errors"]:
        print(f"[ERROR] {err}", file=sys.stderr)
    if not events_out["events"] and not events_out["errors"]:
        print("(該当期間に登録されているイベントはありませんでした)")
    return events_out


def main() -> int:
    parser = argparse.ArgumentParser(description="日本株モーニング11 自動化")
    parser.add_argument("--test", action="store_true", help="データ取得＋スコア計算を試し、結果をログ出力する")
    parser.add_argument("--generate", action="store_true", help="データ取得＋スコア計算＋HTML生成まで行う")
    parser.add_argument("--date", help="過去日付での実行（未実装・後続ステップで対応予定）")
    args = parser.parse_args()

    if args.date:
        print(f"[ERROR] --date は未実装です（指定値: {args.date}）", file=sys.stderr)
        return 1

    if not args.test and not args.generate:
        print("[ERROR] --test または --generate を指定してください", file=sys.stderr)
        return 1

    results, score_out, settings = fetch_and_score()
    events_out = fetch_events(settings)

    if args.generate:
        path = generate_report(results, score_out, settings, events_out)
        print(f"[OK] HTML生成: {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

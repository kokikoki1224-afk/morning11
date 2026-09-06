"""日本株モーニング11 自動化 - エントリポイント。

現時点のスコープ（STEP2+STEP3）: Nasdaq / SOX / 米10年債利回り / USD/JPY / KOSPI の
自動取得、および ±0.2%ルール・bp閾値ルールによるスコア計算まで。
AI分析・HTML生成・GitHub Actions/Pagesは未実装（後続ステップ）。

使い方:
    python main.py --test              # 5指標の取得＋スコア計算を行い、結果をログ出力
    python main.py --test --date ...   # 過去日付での再実行は未実装（後続ステップで対応）
"""

from __future__ import annotations

import argparse
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from analysis.score import compute_score
from data import market


def log_result(result: market.MetricResult) -> None:
    if result.status == "OK":
        if result.change_bp is not None:
            change_str = f"{result.value} ({result.change:+.3f}pt / {result.change_bp:+.1f}bp)"
        else:
            change_str = f"{result.value} ({result.change:+.2f} / {result.change_percent:+.2f}%)"
        print(f"[OK] {result.label}: {change_str}  source={result.source} date={result.timestamp}")
    else:
        print(f"[ERROR] {result.label} data unavailable ({result.error})", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="日本株モーニング11 自動化")
    parser.add_argument("--test", action="store_true", help="データ取得のみ試して結果をログ出力する")
    parser.add_argument("--date", help="過去日付での実行（未実装・後続ステップで対応予定）")
    args = parser.parse_args()

    if args.date:
        print(f"[ERROR] --date は未実装です（指定値: {args.date}）", file=sys.stderr)
        return 1

    if not args.test:
        print("[ERROR] 現時点では --test のみ対応しています（STEP2）", file=sys.stderr)
        return 1

    settings = market.load_settings()
    results = market.fetch_all(settings)
    ok_count = 0
    for key in ("nasdaq", "sox", "us10y", "usdjpy", "kospi"):
        result = results.get(key)
        if result is None:
            print(f"[ERROR] {key}: 設定に見つかりません", file=sys.stderr)
            continue
        log_result(result)
        if result.status == "OK":
            ok_count += 1

    print(f"--- {ok_count}/{len(results)} 指標を取得できました ---")

    score_out = compute_score(results, settings)
    print(f"[OK] Score: {score_out['verdict_label']}")
    for key, m in score_out["metrics"].items():
        if m["status"] == "ok":
            print(f"  {key}: score={m['score']:+d}")
        else:
            print(f"  {key}: unavailable ({m['error']})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

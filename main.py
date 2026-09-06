"""日本株モーニング11 自動化 - エントリポイント。

現時点のスコープ（STEP2+STEP3+STEP4+STEP5+STEP6+STEP7+STEP8-A+STEP8-B-1〜B-3）:
- 海外5指標（Nasdaq/SOX/米10年債/USD-JPY/KOSPI）の自動取得＋±0.2%ルール・bp閾値によるスコア計算
- 日本市場3指標（日経225先物/TOPIX連動ETF/日本10年債）の自動取得（総合スコアには含めない参考情報）
- 今週の重要イベント（米国主要指標はFRED、FOMC/日銀等は静的カレンダー）の取得・JST変換・期間フィルタ
- ニュース候補の取得・直近24時間フィルタ・重複除去・ルールベースのテーマ分類
- 既存デザインのHTMLへの埋め込み（今週の重要イベント・注目ニュースを含む）
- AI分析の入力データ組み立て・プロンプト・モデル切り替え・2モデル比較（--ai / --ai-compare）
AI出力のHTMLへの統合はSTEP8-B-4で対応予定（未実装。--generateではAIを呼ばない）。

使い方:
    python main.py --test              # データ取得＋スコア計算＋イベント/ニュース取得をログ出力
    python main.py --generate          # 上記に加え output/ にHTMLを生成する（AIは呼ばない）
    python main.py --ai                # 設定のai.modelでAI分析を1回実行し、結果を表示
    python main.py --ai-compare        # 同一データ・同一プロンプトで2モデルを実行して比較
    python main.py --test --date ...   # 過去日付での再実行は未実装（後続ステップで対応）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from analysis import ai_analysis
from analysis.score import compute_score
from data import events as events_module
from data import market
from data import news as news_module
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


def fetch_news(settings: dict) -> dict:
    print("[ニュース候補（参考情報・注目ニュース欄は表示選定で絞り込む）]")
    news_out = news_module.fetch_news(settings)
    print(f"--- fetched_at: {news_out['fetched_at']} ---")
    for n in news_out["news"]:
        print(f"[OK] {n.published_at} [{n.category}] {n.source}: {n.title}")
    for err in news_out["errors"]:
        print(f"[ERROR] {err}", file=sys.stderr)
    if not news_out["news"] and not news_out["errors"]:
        print("(直近24時間以内に該当するニュースはありませんでした)")
    return news_out


def _log_ai_result(result: ai_analysis.AiAnalysisResult) -> None:
    header = f"[{result.status.upper()}] model={result.model}"
    if result.elapsed_seconds is not None:
        header += f" elapsed={result.elapsed_seconds}s"
    if result.input_tokens is not None:
        header += f" tokens(in/out)={result.input_tokens}/{result.output_tokens}"
    print(header)
    if result.error:
        print(f"  error: {result.error}", file=sys.stderr)
    for warning in result.warnings:
        print(f"  [WARN] {warning}", file=sys.stderr)
    if result.analysis:
        a = result.analysis
        print(f"  headline: {a.headline}")
        print(f"  lead: {a.lead}")
        print(f"  verdict: {a.verdict}")
        print(f"  combination_analysis: {a.combination_analysis}")
        print(f"  overall_analysis: {a.overall_analysis}")
        for s in a.sector_analysis:
            print(f"  [{s.sector}] {s.view} — {s.reason}")
        for r in a.key_risks:
            print(f"  risk: {r}")
        for e in a.event_watch:
            print(f"  event_watch: {e}")


def run_ai(settings: dict, results: dict, score_out: dict, events_out: dict, news_out: dict,
           compare: bool) -> int:
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    ai_input = ai_analysis.build_ai_input(results, score_out, events_out, news_out, settings, now)

    ok, reason = ai_analysis.should_run_ai(settings)
    if not ok:
        print(f"[ERROR] AI分析を実行できません: {reason}", file=sys.stderr)
        print("  ANTHROPIC_API_KEY を設定してから再実行してください（設定ファイルには書かないこと）", file=sys.stderr)
        return 1

    if compare:
        models = settings.get("ai", {}).get("compare_models", [])
        print(f"[モデル比較（同一データ・同一プロンプト、変えるのはモデルのみ）: {', '.join(models)}]")
        print(f"--- 入力データ対象日時: {ai_input['report_datetime_jst']} JST ---")
        print("--- 注意: モデル数だけAPIコストがかかります ---")
        ai_results = ai_analysis.compare_models(ai_input, settings)
    else:
        model = settings.get("ai", {}).get("model")
        print(f"[AI分析: model={model}]")
        print(f"--- 入力データ対象日時: {ai_input['report_datetime_jst']} JST ---")
        ai_results = [ai_analysis.run_ai_analysis(ai_input, settings)]

    for result in ai_results:
        print()
        _log_ai_result(result)

    output_dir = Path(__file__).resolve().parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"ai_{'compare' if compare else 'run'}_{now.strftime('%Y-%m-%d')}.json"
    out_path.write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "ai_input": ai_input,
                "results": [r.to_dict() for r in ai_results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n[OK] 結果を保存しました: {out_path}")
    print("（STEP8-B-4でHTMLへ統合するまでは、この出力をローカルで見比べる用途です）")

    return 0 if any(r.status == "ok" for r in ai_results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="日本株モーニング11 自動化")
    parser.add_argument("--test", action="store_true", help="データ取得＋スコア計算を試し、結果をログ出力する")
    parser.add_argument("--generate", action="store_true", help="データ取得＋スコア計算＋HTML生成まで行う（AIは呼ばない）")
    parser.add_argument("--ai", action="store_true", help="設定のai.modelでAI分析を1回実行する")
    parser.add_argument("--ai-compare", action="store_true", help="同一データ・同一プロンプトで複数モデルを実行して比較する")
    parser.add_argument("--date", help="過去日付での実行（未実装・後続ステップで対応予定）")
    args = parser.parse_args()

    if args.date:
        print(f"[ERROR] --date は未実装です（指定値: {args.date}）", file=sys.stderr)
        return 1

    if not (args.test or args.generate or args.ai or args.ai_compare):
        print("[ERROR] --test / --generate / --ai / --ai-compare のいずれかを指定してください", file=sys.stderr)
        return 1

    results, score_out, settings = fetch_and_score()
    events_out = fetch_events(settings)
    news_out = fetch_news(settings)

    if args.generate:
        path = generate_report(results, score_out, settings, events_out, news_out)
        print(f"[OK] HTML生成: {path}")

    if args.ai or args.ai_compare:
        return run_ai(settings, results, score_out, events_out, news_out, compare=args.ai_compare)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

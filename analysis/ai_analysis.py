"""AI分析（STEP8-B-1〜B-3）。

役割分離の原則（STEP2〜8-Aから維持）:
- 取得は data/*.py、計算・スコアは analysis/score.py、表示は generator/html.py の責務。
- ここでのAIの役割は「確定済みの数値を解釈すること」だけ。AIには前日比・bp・閾値判定・
  スコア・JST変換・ニュースの重複除去などを一切計算させない（すべてPython側で確定済み）。
- モデルIDはコードにハードコードせず config/settings.json の `ai.model` から読む。
  OpusとSonnetを「同じ日の同じ入力データ・同じプロンプト」で比較できるよう、
  モデル以外の条件は完全に同一にしている。

STEP8-B-4（HTMLへの本格統合）はまだ行わない。ここでは入力データ組み立て・プロンプト・
モデル切り替え・2モデル比較までを提供する。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "ai_analysis_system.md"

# AI出力に出てきたら入力データ内に実在するか検証する数値パターン
# （%・bp・円・カンマ区切りの大きな数字。年号や箇条書き番号は対象外）
_NUMERIC_PATTERNS = [
    re.compile(r"[+\-−]?\d[\d,]*\.?\d*\s*%"),
    re.compile(r"[+\-−]?\d[\d,]*\.?\d*\s*bp"),
    re.compile(r"\d[\d,]*\.?\d*\s*円"),
    re.compile(r"\d{1,3}(?:,\d{3})+"),
]


# --- AI出力のスキーマ（構造化JSON） ---------------------------------------

class SectorView(BaseModel):
    sector: str = Field(description="入力のsectorsで指定されたセクター名をそのまま使う")
    view: Literal["追い風", "逆風", "中立", "注目"]
    reason: str


class AiAnalysis(BaseModel):
    headline: str
    lead: str
    verdict: str
    combination_analysis: str
    overall_analysis: str
    sector_analysis: list[SectorView]
    key_risks: list[str]
    event_watch: list[str]


@dataclass
class AiAnalysisResult:
    """1モデル分の実行結果。比較モードではこれを並べて見比べる。"""

    status: Literal["ok", "skipped", "error"]
    model: str | None = None
    analysis: AiAnalysis | None = None
    elapsed_seconds: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "model": self.model,
            "analysis": self.analysis.model_dump() if self.analysis else None,
            "elapsed_seconds": self.elapsed_seconds,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "error": self.error,
            "warnings": list(self.warnings),
        }


# --- STEP8-B-1: AIへの入力データ ------------------------------------------

def _metric_to_input(result: Any, *, scored: bool, score: int | None = None) -> dict[str, Any]:
    if result.status != "OK":
        return {
            "label": result.label,
            "status": "取得失敗",
            "error": result.error,
            "scored": scored,
        }
    entry: dict[str, Any] = {
        "label": result.label,
        "status": "OK",
        "value": result.value,
        "previous_value": result.previous_value,
        "source": result.source,
        "timestamp": result.timestamp,
        "scored": scored,
    }
    if result.change_bp is not None:
        entry["change_bp"] = result.change_bp
    if result.change_percent is not None:
        entry["change_percent"] = result.change_percent
    if scored and score is not None:
        entry["score"] = score
    return entry


def build_ai_input(
    metric_results: dict[str, dict[str, Any]],
    score_out: dict[str, Any],
    events_out: dict[str, Any],
    news_out: dict[str, Any],
    settings: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """AIに渡す入力データを組み立てる（ネットワークアクセスなし・純粋関数）。

    AIはこのJSONに書かれていることしか知らない状態にする。取得失敗や未実装も
    明示的に含めることで、「無いデータを想像で埋める」余地をなくす。
    """
    ai_settings = settings.get("ai", {})
    max_news = ai_settings.get("max_news_items", 30)

    overseas = []
    for key, result in metric_results.get("overseas", {}).items():
        metric_score = score_out.get("metrics", {}).get(key, {})
        overseas.append(
            _metric_to_input(
                result,
                scored=True,
                score=metric_score.get("score") if metric_score.get("status") == "ok" else None,
            )
        )

    japan = [
        _metric_to_input(result, scored=False)
        for result in metric_results.get("japan", {}).values()
    ]

    news_items = news_out.get("news", [])[:max_news]

    return {
        "report_datetime_jst": now.strftime("%Y-%m-%d %H:%M"),
        "edition": "週末版" if now.weekday() >= 5 else "平日版",
        "sectors": ai_settings.get("sectors", []),
        "overseas_scored_metrics": overseas,
        "japan_reference_metrics": japan,
        "not_implemented_metrics": ai_settings.get(
            "not_implemented_metrics", ["S&P500", "NYダウ", "日経平均"]
        ),
        "score": {
            "total_score": score_out.get("score"),
            "available": score_out.get("available"),
            "total": score_out.get("total"),
            "provisional": score_out.get("provisional"),
            "verdict": score_out.get("verdict"),
            "rule": {
                "percent_threshold": settings.get("score_threshold_percent"),
                "bond_bp_threshold": settings.get("bond_yield_threshold_bp"),
                "note": "スコアはPython側で確定済み。再計算しないこと",
            },
        },
        "events": {
            "window_start": events_out.get("window_start"),
            "window_end": events_out.get("window_end"),
            "items": [e.to_dict() for e in events_out.get("events", [])],
            "errors": events_out.get("errors", []),
        },
        "news": [n.to_dict() for n in news_items],
        "data_errors": list(events_out.get("errors", [])) + list(news_out.get("errors", [])),
    }


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


# --- 数値ガード（AIが入力に無い数値を書いていないか検証） --------------------

def _extract_numeric_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for pattern in _NUMERIC_PATTERNS:
        for match in pattern.findall(text):
            tokens.add(re.sub(r"\s+", "", match).lstrip("+"))
    return tokens


def validate_numbers(analysis: AiAnalysis, ai_input: dict[str, Any]) -> list[str]:
    """AI出力中の数値が入力データに実在するかを検証し、疑わしいものを警告として返す。

    ここでは文章を破棄せず警告にとどめる（比較段階では出力そのものを見たいため）。
    HTML統合時（STEP8-B-4）に、警告付きフィールドを採用するかどうかを判断する。
    """
    haystack = json.dumps(ai_input, ensure_ascii=False)
    haystack_digits = re.sub(r"[,\s]", "", haystack)

    warnings: list[str] = []
    text_fields = {
        "headline": analysis.headline,
        "lead": analysis.lead,
        "verdict": analysis.verdict,
        "combination_analysis": analysis.combination_analysis,
        "overall_analysis": analysis.overall_analysis,
    }
    for sector in analysis.sector_analysis:
        text_fields[f"sector:{sector.sector}"] = sector.reason
    for i, risk in enumerate(analysis.key_risks):
        text_fields[f"key_risks[{i}]"] = risk
    for i, watch in enumerate(analysis.event_watch):
        text_fields[f"event_watch[{i}]"] = watch

    for field_name, text in text_fields.items():
        for token in _extract_numeric_tokens(text):
            digits = re.sub(r"[,\s+\-−%]|bp|円", "", token)
            if not digits:
                continue
            if digits not in haystack_digits:
                warnings.append(f"{field_name}: 入力データに見当たらない数値 '{token}'")
    return warnings


# --- STEP8-B-2: モデル切り替え・実行 ---------------------------------------

def should_run_ai(settings: dict[str, Any]) -> tuple[bool, str | None]:
    """AI分析を実行してよいかを判定する（キーがあれば自動実行の方針）。"""
    if not settings.get("ai", {}).get("enabled", True):
        return False, "設定で ai.enabled が false になっています"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, "ANTHROPIC_API_KEY が設定されていません"
    return True, None


def run_ai_analysis(
    ai_input: dict[str, Any],
    settings: dict[str, Any],
    model: str | None = None,
) -> AiAnalysisResult:
    """1モデル分のAI分析を実行する。失敗しても例外を投げず、必ず結果オブジェクトを返す。

    model を指定しない場合は settings["ai"]["model"] を使う（コードにモデルIDを
    ハードコードしない）。比較時はここだけを差し替え、他の条件は完全に同一にする。
    """
    ai_settings = settings.get("ai", {})
    model_id = model or ai_settings.get("model")
    if not model_id:
        return AiAnalysisResult(status="error", error="設定に ai.model がありません")

    ok, reason = should_run_ai(settings)
    if not ok:
        return AiAnalysisResult(status="skipped", model=model_id, error=reason)

    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - 依存が入っていない環境向け
        return AiAnalysisResult(status="error", model=model_id, error=f"anthropic未インストール: {exc}")

    client = anthropic.Anthropic(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        timeout=float(ai_settings.get("timeout_seconds", 120)),
        # リトライはSDK既定（2回・指数バックオフ。408/409/429/5xx/接続エラー）に任せる
    )

    started = time.monotonic()
    try:
        response = client.messages.parse(
            model=model_id,
            max_tokens=ai_settings.get("max_tokens", 8000),
            system=load_system_prompt(),
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(ai_input, ensure_ascii=False, indent=2),
                }
            ],
            output_format=AiAnalysis,
        )
    except Exception as exc:  # noqa: BLE001 - AI失敗でアプリ全体を止めないため広く捕捉
        return AiAnalysisResult(
            status="error",
            model=model_id,
            elapsed_seconds=round(time.monotonic() - started, 2),
            error=f"{type(exc).__name__}: {exc}",
        )

    elapsed = round(time.monotonic() - started, 2)

    if getattr(response, "stop_reason", None) == "refusal":
        return AiAnalysisResult(
            status="error", model=model_id, elapsed_seconds=elapsed, error="APIが応答を拒否しました（refusal）"
        )

    analysis = getattr(response, "parsed_output", None)
    if analysis is None:
        return AiAnalysisResult(
            status="error", model=model_id, elapsed_seconds=elapsed, error="構造化出力のパースに失敗しました"
        )

    usage = getattr(response, "usage", None)
    return AiAnalysisResult(
        status="ok",
        model=model_id,
        analysis=analysis,
        elapsed_seconds=elapsed,
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        warnings=validate_numbers(analysis, ai_input),
    )


# --- STEP8-B-3: モデル比較 --------------------------------------------------

def compare_models(
    ai_input: dict[str, Any],
    settings: dict[str, Any],
    models: list[str] | None = None,
) -> list[AiAnalysisResult]:
    """同一の入力データ・同一プロンプトで複数モデルを順に実行する（変えるのはモデルのみ）。

    APIコストは実行したモデル数だけかかる点に注意（README参照）。
    """
    ai_settings = settings.get("ai", {})
    target_models = models or ai_settings.get("compare_models", [])
    return [run_ai_analysis(ai_input, settings, model=m) for m in target_models]

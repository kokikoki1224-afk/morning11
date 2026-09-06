"""スコア計算（STEP3）。

役割分離の原則:
- 市場データの取得（前日比・変化率の算出まで）は data/market.py の責務。
- ここでは data/market.py が返した MetricResult を受け取り、
  ±0.2%ルール／bp閾値ルールに基づく +1/0/-1 判定と合計・総合判定だけを行う。
  ネットワークアクセスやAPI呼び出しは一切行わない（テスト容易性のため）。

欠損データの扱い（重要）:
- 取得に失敗した指標（MetricResult.status != "OK"）は推測値で埋めず、
  スコア計算から除外する（合計にも指標数にも加算しない）。
- 5指標中いくつが実際に使えたかを available/total として必ず保持し、
  一部欠損のまま総合判定だけを断定的に表示しない（provisional フラグで明示）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from data.market import MetricResult

# スコア対象の5指標（S&P500・NYダウ・日本側指標は参考表示のみでスコア対象外）
SCORED_METRICS = ("nasdaq", "sox", "us10y", "usdjpy", "kospi")

# 金利は「低下がプラス」と符号が逆転する指標
INVERTED_METRICS = {"us10y"}

TAILWIND_MIN = 3
HEADWIND_MAX = -3


@dataclass
class MetricScore:
    key: str
    label: str
    status: str  # "ok" or "unavailable"
    change_percent: float | None = None
    change_bp: float | None = None
    score: int | None = None  # unavailableの場合は None（0ではない点に注意）
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def classify_by_threshold(value: float, threshold: float, invert: bool = False) -> int:
    """value(%変化 または bp変化) を threshold と比較して +1/0/-1 を返す。

    invert=False（株価指数・為替の通常ケース）:
        value >= threshold  -> +1（上昇）
        value <= -threshold -> -1（下落）
        それ以外            -> 0
    invert=True（金利など、下落がプラス材料の指標）:
        上の判定結果の符号を反転する。
        例: 米10年債 -4bp (threshold=3) は通常判定で「-1」相当だが、
            金利低下は好材料なので反転して +1 にする。
    """
    if value >= threshold:
        base = 1
    elif value <= -threshold:
        base = -1
    else:
        base = 0
    return -base if invert else base


def _score_one(key: str, result: MetricResult, settings: dict[str, Any]) -> MetricScore:
    label = result.label or key
    if result.status != "OK":
        return MetricScore(key=key, label=label, status="unavailable", error=result.error)

    if key in INVERTED_METRICS:
        threshold_bp = settings["bond_yield_threshold_bp"]
        change_bp = result.change_bp
        if change_bp is None:
            return MetricScore(
                key=key, label=label, status="unavailable", error="change_bp missing"
            )
        score = classify_by_threshold(change_bp, threshold_bp, invert=True)
        return MetricScore(
            key=key, label=label, status="ok", change_bp=change_bp, score=score
        )

    threshold_pct = settings["score_threshold_percent"]
    change_percent = result.change_percent
    if change_percent is None:
        return MetricScore(
            key=key, label=label, status="unavailable", error="change_percent missing"
        )
    score = classify_by_threshold(change_percent, threshold_pct, invert=False)
    return MetricScore(
        key=key, label=label, status="ok", change_percent=change_percent, score=score
    )


def classify_total(total_score: int) -> str:
    if total_score >= TAILWIND_MIN:
        return "追い風"
    if total_score <= HEADWIND_MAX:
        return "逆風"
    return "中立・やや方向感なし"


def compute_score(
    metric_results: dict[str, MetricResult], settings: dict[str, Any]
) -> dict[str, Any]:
    """5指標のMetricResultからスコア計算結果一式を組み立てる。"""
    metrics: dict[str, MetricScore] = {}
    for key in SCORED_METRICS:
        result = metric_results.get(key)
        if result is None:
            metrics[key] = MetricScore(
                key=key, label=key, status="unavailable", error="not fetched"
            )
            continue
        metrics[key] = _score_one(key, result, settings)

    available_scores = [m.score for m in metrics.values() if m.status == "ok"]
    total = len(SCORED_METRICS)
    available = len(available_scores)
    score = sum(available_scores)  # 欠損指標は加算しない（0を足すのとは異なる）
    provisional = available < total

    verdict = classify_total(score)
    if provisional:
        verdict_label = f"{available}/{total}指標取得・暫定スコア {score:+d}（{verdict}）"
    else:
        verdict_label = f"スコア {score:+d}（{verdict}）"

    return {
        "metrics": {k: v.to_dict() for k, v in metrics.items()},
        "score": score,
        "available": available,
        "total": total,
        "provisional": provisional,
        "verdict": verdict,
        "verdict_label": verdict_label,
    }

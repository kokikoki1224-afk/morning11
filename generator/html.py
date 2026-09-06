"""HTML生成（STEP4 + STEP5）。

役割分離の原則:
- 市場データの取得は data/market.py、スコア計算は analysis/score.py の責務。
- ここではその結果を受け取り、既存の「日本株モーニング11」のデザイン・CSS・レイアウトを
  一切変更せず、templates/morning11.html のプレースホルダーに値を埋め込むだけを行う。
- AIによる文章解釈（見出し・組み合わせ分析・セクター分析等）はまだ実装しない。
  該当箇所は機械的な定型文（後続ステップで実装予定の旨を明記）にとどめる。
- 取得失敗・未実装の指標は、推測値で埋めず「取得失敗」「未実装」と明示する。
- 日本市場3指標（日経225先物・TOPIX連動ETF・日本10年債）はSTEP5で追加した参考情報で、
  総合スコア（analysis/score.py が扱うのは overseas 側の5指標のみ）には一切影響しない。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from analysis.score import SCORED_METRICS, classify_by_threshold
from data.market import MetricResult

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "morning11.html"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
JST = ZoneInfo("Asia/Tokyo")
MINUS = "−"  # 既存デザインが使っている全角寄りのマイナス記号(U+2212)

WEEKDAY_JA = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

# STEP2/3で自動取得・スコア計算まで実装済みの海外5指標（総合スコア対象）
SCORED_ROW_PREFIXES: dict[str, tuple[str, bool]] = {
    "nasdaq": ("NASDAQ", False),
    "sox": ("SOX", False),
    "us10y": ("US10Y", True),
    "usdjpy": ("USDJPY", False),
    "kospi": ("KOSPI", False),
}

# STEP5で自動取得を追加した日本市場3指標（参考情報・総合スコア対象外）
REFERENCE_ROW_PREFIXES: dict[str, tuple[str, bool]] = {
    "nikkei_futures": ("NKFUT", False),
    "topix_etf": ("TOPIX", False),
    "japan10y": ("JGB10Y", True),
}

# 既存HTMLのカード構造は維持するが、現時点では自動取得の対象外（後続ステップで対応）
NOT_IMPLEMENTED_PREFIXES = ["SP500", "DOW", "NIKKEI"]


def fmt_signed(value: float, decimals: int, suffix: str) -> str:
    sign = "+" if value >= 0 else MINUS
    return f"{sign}{abs(value):.{decimals}f}{suffix}"


def fmt_plain(value: float, decimals: int) -> str:
    return f"{value:,.{decimals}f}"


def _not_implemented_tokens(prefix: str) -> dict[str, str]:
    return {
        f"{prefix}_STRIPE_CLASS": "",
        f"{prefix}_VALUE": "未実装",
        f"{prefix}_FIG_CLASS": "flat",
        f"{prefix}_CHANGE": "未実装",
        f"{prefix}_NOTE": "後続ステップで自動取得を実装予定です（現時点では未対応のため数値は表示していません）。",
        f"{prefix}_IMPACT_CLASS": "",
        f"{prefix}_IMPACT": "未実装のため評価・スコア対象外です。",
    }


def _unavailable_tokens(prefix: str, error: str | None) -> dict[str, str]:
    reason = error or "不明なエラー"
    return {
        f"{prefix}_STRIPE_CLASS": "",
        f"{prefix}_VALUE": "取得失敗",
        f"{prefix}_FIG_CLASS": "flat",
        f"{prefix}_CHANGE": "取得失敗",
        f"{prefix}_NOTE": f"データ取得に失敗しました（{reason}）。推測値は使用せず、スコアにも加算していません。",
        f"{prefix}_IMPACT_CLASS": "",
        f"{prefix}_IMPACT": "取得失敗のため評価対象外です。",
    }


def _score_stripe_impact_class(score: int) -> tuple[str, str]:
    if score > 0:
        return "tail", "tail"
    if score < 0:
        return "head", "head"
    return "", ""


def _raw_direction_class(raw_dir: int) -> str:
    return {1: "up", -1: "down", 0: "flat"}[raw_dir]


def _format_value_and_change(
    result: MetricResult, is_yield: bool, settings: dict[str, Any]
) -> tuple[str, str, int]:
    if is_yield:
        value_str = f"{result.value:.3f}%"
        change_val = result.change_bp or 0.0
        change_str = fmt_signed(change_val, 1, "bp")
        raw_dir = classify_by_threshold(change_val, settings["bond_yield_threshold_bp"])
    else:
        value_str = fmt_plain(result.value, 2)
        change_val = result.change_percent or 0.0
        change_str = fmt_signed(change_val, 2, "%")
        raw_dir = classify_by_threshold(change_val, settings["score_threshold_percent"])
    return value_str, change_str, raw_dir


def _ok_tokens(
    prefix: str,
    result: MetricResult,
    score: int,
    is_yield: bool,
    settings: dict[str, Any],
) -> dict[str, str]:
    value_str, change_str, raw_dir = _format_value_and_change(result, is_yield, settings)
    fig_class = _raw_direction_class(raw_dir)
    stripe_class, impact_class = _score_stripe_impact_class(score)

    note = (
        f"前日比 {change_str}（{result.timestamp}時点、source={result.source}）。"
        "材料の解説（AIによる分析）は後続ステップで実装予定です。"
    )
    impact = (
        f"スコア判定: {score:+d}。セクターへの影響分析（AIによる解釈）は後続ステップで実装予定です。"
    )

    return {
        f"{prefix}_STRIPE_CLASS": stripe_class,
        f"{prefix}_VALUE": value_str,
        f"{prefix}_FIG_CLASS": fig_class,
        f"{prefix}_CHANGE": change_str,
        f"{prefix}_NOTE": note,
        f"{prefix}_IMPACT_CLASS": impact_class,
        f"{prefix}_IMPACT": impact,
    }


def _reference_ok_tokens(
    prefix: str,
    result: MetricResult,
    is_yield: bool,
    settings: dict[str, Any],
) -> dict[str, str]:
    """日本市場3指標(参考情報・スコア対象外)用。stripe/fig-cは生の騰落方向のみで判定する。"""
    value_str, change_str, raw_dir = _format_value_and_change(result, is_yield, settings)
    fig_class = _raw_direction_class(raw_dir)
    stripe_class = {"up": "tail", "down": "head", "flat": ""}[fig_class]

    note = (
        f"前日比 {change_str}（{result.timestamp}時点、source={result.source}）。"
        "参考情報として表示しており、総合スコアには含まれません。詳しい解説はAI分析実装後に追加予定です。"
    )
    impact = "参考情報（総合スコアには含まれません）。セクターへの影響分析は後続ステップで実装予定です。"

    return {
        f"{prefix}_STRIPE_CLASS": stripe_class,
        f"{prefix}_VALUE": value_str,
        f"{prefix}_FIG_CLASS": fig_class,
        f"{prefix}_CHANGE": change_str,
        f"{prefix}_NOTE": note,
        f"{prefix}_IMPACT_CLASS": "",
        f"{prefix}_IMPACT": impact,
    }


def _pattern_fragment(label: str, metric: dict[str, Any]) -> str:
    if metric["status"] != "ok":
        return f"<span class=\"f\">{label} 取得失敗</span>"
    score = metric["score"]
    cls, arrow = {1: ("u", "↑"), -1: ("d", "↓"), 0: ("f", "→")}[score]
    return f'<span class="{cls}">{label} {arrow}</span>'


def _sample_date(*groups: dict[str, MetricResult], fallback: str) -> str:
    for group in groups:
        for m in group.values():
            if m.status == "OK" and m.timestamp:
                return m.timestamp
    return fallback


def build_context(
    metric_results: dict[str, dict[str, MetricResult]],
    score_out: dict[str, Any],
    settings: dict[str, Any],
    now: datetime,
) -> dict[str, str]:
    overseas = metric_results["overseas"]
    japan = metric_results["japan"]

    weekday = now.weekday()  # 0=Mon .. 6=Sun
    is_weekend = weekday >= 5

    context: dict[str, str] = {
        "DATELINE": f"{now.strftime('%Y.%m.%d')} {WEEKDAY_JA[weekday]} — {now.strftime('%H:%M')} JST",
        "EDITION": "週末版" if is_weekend else "平日版",
    }

    if is_weekend:
        days_to_monday = (7 - weekday) % 7 or 7
        next_monday = now + timedelta(days=days_to_monday)
        context["NEXT_NOTE"] = f"次回{next_monday.strftime('%m/%d')}(月)09:00寄付"
    else:
        context["NEXT_NOTE"] = "本日09:00寄付"

    score = score_out["score"]
    verdict = score_out["verdict"]
    available = score_out["available"]
    total = score_out["total"]

    context["HEADLINE"] = f"自動生成レポート：総合スコア{score:+d}（{verdict}）"
    context["SUBTITLE"] = (
        f"{available}/{total}指標（海外）を自動取得し、±0.2%ルール・bp閾値でスコアを計算した暫定版です。"
        "日本市場3指標（日経225先物・TOPIX連動ETF・日本10年債）は参考情報として掲載していますが、"
        "総合スコアには含まれません。AIによる見出し・解説文の生成はまだ実装されていません（後続ステップで対応予定）。"
    )

    color_map = {"追い風": "var(--up)", "逆風": "var(--down)"}
    context["VERDICT_COLOR"] = color_map.get(verdict, "var(--flat)")
    context["VERDICT_WORD"] = verdict
    context["SCORE_LABEL"] = score_out["verdict_label"]

    parts = []
    for key in SCORED_METRICS:
        m = score_out["metrics"][key]
        if m["status"] == "ok":
            parts.append(f"{m['label']} {m['score']:+d}")
        else:
            parts.append(f"{m['label']} 取得失敗")
    context["VERDICT_THESIS"] = (
        "、".join(parts) + f" の合計で総合スコアは{score:+d}（{verdict}）です。"
        "見出し・解説文はAI分析実装前の機械的な暫定表示のため、材料の詳しい解説は後続ステップで追加されます。"
    )

    clamped_score = max(-5, min(5, score))
    segs = []
    for i in range(-5, 6):
        cls = "seg"
        if i < 0:
            cls += " neg"
        elif i > 0:
            cls += " pos"
        if i == clamped_score:
            cls += " on"
        segs.append(f'<span class="{cls}"></span>')
    context["GAUGE_TRACK"] = "".join(segs)
    context["GAUGE_ARIA"] = f"総合スコア{score:+d}（{verdict}）。レンジはマイナス5からプラス5"

    context["OVERSEAS_TAG"] = f"自動取得（{_sample_date(overseas, fallback=now.strftime('%Y-%m-%d'))}時点）"
    context["JAPAN_TAG"] = f"自動取得（{_sample_date(japan, fallback=now.strftime('%Y-%m-%d'))}時点・参考情報）"

    # 海外5指標（自動取得・スコア計算済み）
    for key, (prefix, is_yield) in SCORED_ROW_PREFIXES.items():
        result = overseas.get(key)
        mscore = score_out["metrics"][key]
        if result is None or result.status != "OK" or mscore["status"] != "ok":
            error = result.error if result else "not fetched"
            context.update(_unavailable_tokens(prefix, error))
        else:
            context.update(_ok_tokens(prefix, result, mscore["score"], is_yield, settings))

    # 日本市場3指標（STEP5で追加、参考情報・スコア対象外）
    for key, (prefix, is_yield) in REFERENCE_ROW_PREFIXES.items():
        result = japan.get(key)
        if result is None or result.status != "OK":
            error = result.error if result else "not fetched"
            context.update(_unavailable_tokens(prefix, error))
        else:
            context.update(_reference_ok_tokens(prefix, result, is_yield, settings))

    # 現時点で自動取得の対象外（後続ステップで対応）：S&P500・NYダウ・日経平均
    for prefix in NOT_IMPLEMENTED_PREFIXES:
        context.update(_not_implemented_tokens(prefix))

    # 組み合わせの読み（機械的に生成、AI解釈はまだ実装しない。海外5指標のみ・日本側は含めない）
    context["PATTERN_LINE"] = " ／ ".join(
        _pattern_fragment(score_out["metrics"][key]["label"], score_out["metrics"][key])
        for key in SCORED_METRICS
    )
    context["COMBO_TEXT"] = (
        "AIによる組み合わせ分析・セクター影響分析は未実装です（後続ステップで実装予定）。"
        "各指標のスコア内訳は下記「スコアの付け方」セクションをご確認ください。"
    )

    # 今週の重要イベント（STEP6で自動取得予定、現時点では未実装）
    context["EVENTS_TAG"] = "未実装"
    context["EVENTS_BLOCK"] = (
        '      <div class="ev">\n'
        '        <div class="ev-d num">—</div>\n'
        '        <div class="ev-b">\n'
        '          <div class="ev-t">イベントカレンダーは未実装です</div>\n'
        '          <div class="ev-n">経済指標・中央銀行イベントの自動取得はSTEP6以降で実装予定です。</div>\n'
        "        </div>\n"
        "      </div>"
    )

    # スコアの付け方（データから機械的に生成、対象は海外5指標のみ）
    context["BOND_THRESHOLD_BP"] = str(settings["bond_yield_threshold_bp"])
    bullets = [
        f"<li>今回：{'／'.join(parts)} ＝ <strong>合計 {score:+d}（{verdict}）</strong></li>"
    ]
    for key in SCORED_METRICS:
        m = score_out["metrics"][key]
        if m["status"] == "ok":
            detail = f"{m['change_bp']:+.1f}bp" if m.get("change_bp") is not None else f"{m['change_percent']:+.2f}%"
            bullets.append(f"<li><strong>{m['label']}は{detail}のため{m['score']:+d}</strong></li>")
        else:
            bullets.append(f"<li><strong>{m['label']}は取得失敗のためスコア対象外</strong></li>")
    if score_out["provisional"]:
        bullets.append(
            f"<li><strong>{available}/{total}指標のみ取得できたため、暫定スコアとして表示しています。</strong></li>"
        )
    bullets.append(
        "<li>日本市場3指標（日経225先物・TOPIX連動ETF・日本10年債）は参考情報として表示していますが、"
        "総合スコアには含めていません。</li>"
    )
    bullets.append(
        "<li>S&amp;P500・NYダウ・日経平均は現時点で自動取得未実装のため「未実装」と表示し、スコアには含めていません。</li>"
    )
    context["SCORE_RULE_BULLETS"] = "\n".join(f"        {b}" for b in bullets)

    context["GENERATED_AT"] = now.strftime("%Y-%m-%d %H:%M JST")

    return context


def render(template: str, context: dict[str, str]) -> str:
    html = template
    for key, value in context.items():
        html = html.replace(f"{{{{{key}}}}}", value)
    return html


def generate_report(
    metric_results: dict[str, dict[str, MetricResult]],
    score_out: dict[str, Any],
    settings: dict[str, Any],
    output_dir: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """テンプレートに値を埋め込み、output/YYYY-MM-DD.html と output/index.html を書き出す。"""
    now = now or datetime.now(JST)
    output_dir = output_dir or DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    context = build_context(metric_results, score_out, settings, now)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = render(template, context)

    dated_path = output_dir / f"{now.strftime('%Y-%m-%d')}.html"
    index_path = output_dir / "index.html"
    dated_path.write_text(html, encoding="utf-8")
    index_path.write_text(html, encoding="utf-8")
    return dated_path

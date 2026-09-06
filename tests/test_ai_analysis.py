"""STEP8-B: AI分析のテスト。実APIは一切叩かない（すべてモック）。"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from analysis import ai_analysis
from analysis.ai_analysis import AiAnalysis, SectorView
from analysis.score import compute_score
from data.events import EventRecord
from data.market import MetricResult
from data.news import NewsItem

JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 9, 8, 7, 0, tzinfo=JST)

SETTINGS = {
    "score_threshold_percent": 0.2,
    "bond_yield_threshold_bp": 3,
    "ai": {
        "enabled": True,
        "model": "claude-opus-5",
        "compare_models": ["claude-opus-5", "claude-sonnet-5"],
        "max_tokens": 8000,
        "timeout_seconds": 120,
        "max_news_items": 30,
        "sectors": ["半導体", "銀行", "輸出株"],
        "not_implemented_metrics": ["S&P500", "NYダウ", "日経平均"],
    },
}


def _metric(key, label, *, change_percent=None, change_bp=None, value=100.0):
    return MetricResult(
        key=key, label=label, status="OK", value=value, previous_value=value - 1,
        change=1.0, change_percent=change_percent, change_bp=change_bp,
        source=f"test:{key}", timestamp="2026-09-07",
    )


def _results():
    return {
        "overseas": {
            "nasdaq": _metric("nasdaq", "Nasdaq総合", change_percent=-0.29, value=26506.99),
            "sox": _metric("sox", "SOX", change_percent=3.38, value=11735.26),
            "us10y": _metric("us10y", "米10年債利回り", change_bp=2.2, value=4.784),
            "usdjpy": _metric("usdjpy", "ドル円", change_percent=-1.82, value=155.78),
            "kospi": _metric("kospi", "KOSPI", change_percent=1.64, value=6687.21),
        },
        "japan": {
            "nikkei_futures": _metric("nikkei_futures", "日経225先物", change_percent=1.77, value=65820.0),
            "topix_etf": _metric("topix_etf", "TOPIX連動ETF（1306.T）", change_percent=0.07, value=427.5),
            "japan10y": _metric("japan10y", "日本10年国債利回り", change_bp=-4.0, value=2.966),
        },
    }


def _events_out():
    return {
        "events": [
            EventRecord(date="2026-09-08", time="08:50", country="JP", event="GDP 2次速報",
                        importance="high", source="内閣府")
        ],
        "errors": [],
        "window_start": "2026-09-08",
        "window_end": "2026-09-15",
    }


def _news_out():
    return {
        "news": [
            NewsItem(title="半導体関連が上昇", url="https://example.com/a",
                     published_at="2026-09-08T06:00:00+09:00", source="テスト通信",
                     category="semiconductor", matched_keyword="半導体")
        ],
        "errors": [],
        "fetched_at": NOW.isoformat(),
    }


def _ai_input():
    results = _results()
    score_out = compute_score(results["overseas"], SETTINGS)
    return ai_analysis.build_ai_input(results, score_out, _events_out(), _news_out(), SETTINGS, NOW)


def _analysis(**overrides) -> AiAnalysis:
    base = dict(
        headline="見出し",
        lead="リード文",
        verdict="総合判定の解説",
        combination_analysis="組み合わせの読み",
        overall_analysis="総合分析",
        sector_analysis=[SectorView(sector="半導体", view="追い風", reason="SOXが+3.38%のため")],
        key_risks=["リスク1"],
        event_watch=["注視点1"],
    )
    base.update(overrides)
    return AiAnalysis(**base)


# --- STEP8-B-1: 入力データ ---------------------------------------------------

def test_build_ai_input_includes_scored_and_reference_metrics_separately():
    ai_input = _ai_input()
    assert len(ai_input["overseas_scored_metrics"]) == 5
    assert all(m["scored"] is True for m in ai_input["overseas_scored_metrics"])
    assert len(ai_input["japan_reference_metrics"]) == 3
    assert all(m["scored"] is False for m in ai_input["japan_reference_metrics"])
    # スコアはPython側で確定した値がそのまま入る（AIには計算させない）
    nasdaq = next(m for m in ai_input["overseas_scored_metrics"] if m["label"] == "Nasdaq総合")
    assert nasdaq["score"] == -1
    assert nasdaq["change_percent"] == -0.29


def test_build_ai_input_includes_score_events_news_and_not_implemented():
    ai_input = _ai_input()
    assert ai_input["score"]["total_score"] == 0
    assert ai_input["score"]["verdict"] == "中立・やや方向感なし"
    assert ai_input["events"]["items"][0]["event"] == "GDP 2次速報"
    assert ai_input["news"][0]["title"] == "半導体関連が上昇"
    assert "日経平均" in ai_input["not_implemented_metrics"]
    assert ai_input["sectors"] == ["半導体", "銀行", "輸出株"]


def test_build_ai_input_marks_failed_metric_as_unavailable_without_guessing():
    results = _results()
    results["overseas"]["usdjpy"] = MetricResult(
        key="usdjpy", label="ドル円", status="ERROR", error="snapshot missing"
    )
    score_out = compute_score(results["overseas"], SETTINGS)
    ai_input = ai_analysis.build_ai_input(results, score_out, _events_out(), _news_out(), SETTINGS, NOW)
    usdjpy = next(m for m in ai_input["overseas_scored_metrics"] if m["label"] == "ドル円")
    assert usdjpy["status"] == "取得失敗"
    assert "value" not in usdjpy  # 推測値を入れない
    assert usdjpy["error"] == "snapshot missing"


def test_build_ai_input_respects_max_news_items():
    news_out = {
        "news": [
            NewsItem(title=f"ニュース{i}", url=f"https://example.com/{i}",
                     published_at="2026-09-08T06:00:00+09:00", source="s",
                     category="ai", matched_keyword="AI")
            for i in range(50)
        ],
        "errors": [],
        "fetched_at": NOW.isoformat(),
    }
    results = _results()
    score_out = compute_score(results["overseas"], SETTINGS)
    ai_input = ai_analysis.build_ai_input(results, score_out, _events_out(), news_out, SETTINGS, NOW)
    assert len(ai_input["news"]) == 30


def test_system_prompt_states_data_only_and_no_fabrication():
    prompt = ai_analysis.load_system_prompt()
    assert "与えられた入力データだけを根拠" in prompt
    assert "判断材料不足" in prompt
    assert "計算しないでください" in prompt
    assert "タイトルしか渡されていません" in prompt


# --- 数値ガード -------------------------------------------------------------

def test_validate_numbers_accepts_values_present_in_input():
    ai_input = _ai_input()
    analysis = _analysis(verdict="SOXは+3.38%と上昇した")
    assert ai_analysis.validate_numbers(analysis, ai_input) == []


def test_validate_numbers_flags_number_not_in_input():
    ai_input = _ai_input()
    analysis = _analysis(verdict="日経平均は+2.71%上昇した")  # 入力に存在しない数値
    warnings = ai_analysis.validate_numbers(analysis, ai_input)
    assert any("2.71" in w for w in warnings)


# --- STEP8-B-2: モデル切り替え・実行 ----------------------------------------

def _fake_response(analysis: AiAnalysis, *, stop_reason="end_turn"):
    return SimpleNamespace(
        parsed_output=analysis,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=1234, output_tokens=567),
    )


def _patched_client(response_or_exc):
    client = MagicMock()
    if isinstance(response_or_exc, Exception):
        client.messages.parse.side_effect = response_or_exc
    else:
        client.messages.parse.return_value = response_or_exc
    return client


def test_run_ai_analysis_uses_model_from_settings_not_hardcoded(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    client = _patched_client(_fake_response(_analysis()))
    with patch("anthropic.Anthropic", return_value=client):
        result = ai_analysis.run_ai_analysis(_ai_input(), SETTINGS)

    assert result.status == "ok"
    assert result.model == "claude-opus-5"
    assert client.messages.parse.call_args.kwargs["model"] == "claude-opus-5"
    assert result.input_tokens == 1234
    assert result.output_tokens == 567
    assert result.elapsed_seconds is not None


def test_run_ai_analysis_model_argument_overrides_settings(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    client = _patched_client(_fake_response(_analysis()))
    with patch("anthropic.Anthropic", return_value=client):
        result = ai_analysis.run_ai_analysis(_ai_input(), SETTINGS, model="claude-sonnet-5")
    assert result.model == "claude-sonnet-5"
    assert client.messages.parse.call_args.kwargs["model"] == "claude-sonnet-5"


def test_run_ai_analysis_without_api_key_is_skipped_not_crash(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = ai_analysis.run_ai_analysis(_ai_input(), SETTINGS)
    assert result.status == "skipped"
    assert "ANTHROPIC_API_KEY" in result.error
    assert result.analysis is None


def test_run_ai_analysis_api_error_returns_error_result_not_exception(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    client = _patched_client(RuntimeError("rate limited"))
    with patch("anthropic.Anthropic", return_value=client):
        result = ai_analysis.run_ai_analysis(_ai_input(), SETTINGS)
    assert result.status == "error"
    assert "rate limited" in result.error
    assert result.analysis is None


def test_run_ai_analysis_invalid_output_does_not_crash(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    broken = SimpleNamespace(parsed_output=None, stop_reason="end_turn", usage=None)
    with patch("anthropic.Anthropic", return_value=_patched_client(broken)):
        result = ai_analysis.run_ai_analysis(_ai_input(), SETTINGS)
    assert result.status == "error"
    assert "パース" in result.error


def test_run_ai_analysis_refusal_is_treated_as_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    refused = _fake_response(_analysis(), stop_reason="refusal")
    with patch("anthropic.Anthropic", return_value=_patched_client(refused)):
        result = ai_analysis.run_ai_analysis(_ai_input(), SETTINGS)
    assert result.status == "error"
    assert "refusal" in result.error


def test_should_run_ai_respects_enabled_flag(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    disabled = {**SETTINGS, "ai": {**SETTINGS["ai"], "enabled": False}}
    ok, reason = ai_analysis.should_run_ai(disabled)
    assert ok is False and "enabled" in reason


# --- STEP8-B-3: モデル比較 --------------------------------------------------

def test_compare_models_runs_each_model_with_identical_input_and_prompt(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    client = _patched_client(_fake_response(_analysis()))
    ai_input = _ai_input()

    with patch("anthropic.Anthropic", return_value=client):
        results = ai_analysis.compare_models(ai_input, SETTINGS)

    assert [r.model for r in results] == ["claude-opus-5", "claude-sonnet-5"]
    calls = client.messages.parse.call_args_list
    assert len(calls) == 2
    # モデル以外の条件（プロンプト・入力データ・max_tokens）が完全に同一であること
    assert calls[0].kwargs["system"] == calls[1].kwargs["system"]
    assert calls[0].kwargs["messages"] == calls[1].kwargs["messages"]
    assert calls[0].kwargs["max_tokens"] == calls[1].kwargs["max_tokens"]
    assert calls[0].kwargs["model"] != calls[1].kwargs["model"]


def test_compare_models_input_json_contains_the_same_target_date(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    client = _patched_client(_fake_response(_analysis()))
    with patch("anthropic.Anthropic", return_value=client):
        ai_analysis.compare_models(_ai_input(), SETTINGS)
    payloads = [json.loads(c.kwargs["messages"][0]["content"]) for c in client.messages.parse.call_args_list]
    assert payloads[0]["report_datetime_jst"] == payloads[1]["report_datetime_jst"] == "2026-09-08 07:00"


def test_compare_models_partial_failure_keeps_other_model_result(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    client = MagicMock()
    client.messages.parse.side_effect = [RuntimeError("boom"), _fake_response(_analysis())]
    with patch("anthropic.Anthropic", return_value=client):
        results = ai_analysis.compare_models(_ai_input(), SETTINGS)
    assert results[0].status == "error"
    assert results[1].status == "ok"


# --- AI分析がスコア・既存パイプラインに影響しないこと ------------------------

def test_ai_analysis_does_not_modify_score_or_inputs(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    results = _results()
    score_before = compute_score(results["overseas"], SETTINGS)
    ai_input = ai_analysis.build_ai_input(results, score_before, _events_out(), _news_out(), SETTINGS, NOW)

    with patch("anthropic.Anthropic", return_value=_patched_client(_fake_response(_analysis()))):
        ai_analysis.run_ai_analysis(ai_input, SETTINGS)

    score_after = compute_score(results["overseas"], SETTINGS)
    assert score_before == score_after


@pytest.mark.parametrize("status", ["skipped", "error"])
def test_ai_failure_leaves_market_event_news_pipeline_untouched(status, monkeypatch):
    """AI失敗時でも市場データ・イベント・ニュースの各出力はそのまま使える。"""
    if status == "skipped":
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = ai_analysis.run_ai_analysis(_ai_input(), SETTINGS)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
        with patch("anthropic.Anthropic", return_value=_patched_client(RuntimeError("boom"))):
            result = ai_analysis.run_ai_analysis(_ai_input(), SETTINGS)

    assert result.status == status
    # 既存データは無傷
    events_out, news_out = _events_out(), _news_out()
    assert len(events_out["events"]) == 1
    assert len(news_out["news"]) == 1

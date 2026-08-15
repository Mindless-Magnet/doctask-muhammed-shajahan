"""Graceful degradation.

When a model call or an external dependency fails, the system falls back and keeps running instead
of dying with it. Three things are tested here, all of them with real fault injection rather than a
patched-out call:

  * a throttle retries with backoff and succeeds
  * a throttled tier falls back to the next tier down, and the run reports it degraded
  * a model tier that is gone entirely does not fail the run: the deterministic rules still produce
    findings, and the run is honestly marked degraded rather than reporting success

The last one matters most. A success message must only ever mean the output is genuinely in the
state it claims, so a run that lost half its capability has to say so.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

import ledgerline.db as db_module
from ledgerline.config import Settings
from ledgerline.llm.client import (
    FaultPlan,
    FaultyClient,
    ModelRequest,
    ModelUnavailableError,
    ReplayClient,
    TieredModelClient,
)
from ledgerline.models import ItemKind, PendingItem, Run
from ledgerline.service import execute_run
from tests.conftest import MSA_TEXT


class _Recorder:
    """Counts calls and reports which model id each one used."""

    def __init__(self, fail_models: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self._fail = fail_models or set()

    def invoke(self, request: ModelRequest, model_id: str):
        self.calls.append(model_id)
        if model_id in self._fail:
            raise ModelUnavailableError(f"{model_id} is gone")
        from ledgerline.llm.client import ModelResponse, Usage

        return ModelResponse(
            data={"ok": True},
            usage=Usage(
                stage=request.stage,
                model_id=model_id,
                input_tokens=10,
                output_tokens=5,
                latency_ms=1,
                cost_usd=0.0,
            ),
        )


def _request(stage: str = "extract", tier: str = "standard") -> ModelRequest:
    return ModelRequest(
        tier=tier, stage=stage, system="s", user_content="u", output_schema={"type": "object"}
    )


def test_a_throttle_retries_with_backoff_and_succeeds():
    base = 0.01
    settings = Settings(retry_base_delay_seconds=base)
    inner = FaultyClient(_Recorder(), FaultPlan(throttle_stages={"extract": 2}))
    slept: list[float] = []
    client = TieredModelClient(inner, settings, sleep=slept.append)

    response = client.invoke(_request())

    assert response.usage.attempts == 3
    assert len(slept) == 2
    # Exponential backoff with jitter. Asserting slept[1] > slept[0] would be flaky, because the
    # jitter bands overlap; asserting each delay falls in its own band is the real property.
    for attempt, delay in enumerate(slept):
        expected = base * (2**attempt)
        assert expected * 0.5 <= delay <= expected * 1.5, (attempt, delay, expected)
    assert response.usage.degraded_from is None


def test_a_throttled_extract_tier_falls_back_and_says_so():
    """Bedrock throttles one model, not one kind of work, so the fault is keyed by model. The
    standard tier is unavailable; extraction continues on the tier below it and records the drop."""
    settings = Settings(retry_base_delay_seconds=0, max_model_retries=1)
    recorder = _Recorder()
    inner = FaultyClient(recorder, FaultPlan(throttle_models={settings.model_standard: 5}))
    client = TieredModelClient(inner, settings, sleep=lambda _: None)

    response = client.invoke(_request(stage="extract", tier="standard"))

    assert response.usage.degraded_from == "standard"
    assert response.usage.model_id == settings.model_cheap
    # The standard model never reached the underlying client: it was throttled every time.
    assert recorder.calls == [settings.model_cheap]


def test_the_judge_never_falls_back_into_the_extractors_family():
    """The judge exists to be an independent check on extraction, which is why it runs on a
    different model family. Falling back to the extractor's own model would keep the run alive at
    the cost of the only thing the judge is for. It degrades to deterministic-only instead."""
    settings = Settings(retry_base_delay_seconds=0, max_model_retries=1)
    recorder = _Recorder()
    inner = FaultyClient(recorder, FaultPlan(throttle_models={settings.model_deep: 5}))
    client = TieredModelClient(inner, settings, sleep=lambda _: None)

    with pytest.raises(ModelUnavailableError):
        client.invoke(_request(stage="examine_judge", tier="deep"))

    assert settings.model_standard not in recorder.calls
    assert settings.model_cheap not in recorder.calls


def test_the_judge_and_the_extractor_are_different_model_families():
    """A verifier that shares the extractor's architecture shares its failure modes. Asserting the
    families differ keeps a later config change from silently collapsing the two."""
    settings = Settings()
    family = lambda model_id: model_id.split(".")[1]  # noqa: E731
    assert family(settings.model_deep) != family(settings.model_standard)


def test_when_every_tier_is_gone_the_caller_is_told_not_lied_to():
    settings = Settings(retry_base_delay_seconds=0, max_model_retries=1)
    all_models = {settings.model_deep, settings.model_standard, settings.model_cheap}
    client = TieredModelClient(_Recorder(fail_models=all_models), settings, sleep=lambda _: None)

    with pytest.raises(ModelUnavailableError) as exc:
        client.invoke(_request(stage="examine_judge", tier="deep"))
    assert "exhausted tier deep and all fallbacks" in str(exc.value)


def test_the_cost_report_counts_degraded_calls_separately():
    settings = Settings(retry_base_delay_seconds=0, max_model_retries=1)
    inner = FaultyClient(_Recorder(), FaultPlan(throttle_models={settings.model_standard: 5}))
    client = TieredModelClient(inner, settings, sleep=lambda _: None)
    client.invoke(_request())

    report = client.cost_report()
    assert report["by_stage"]["extract"]["degraded_calls"] == 1
    assert report["by_stage"]["extract"]["calls"] == 1
    assert report["prices_pulled_at"]


# --------------------------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------------------------


def test_a_run_survives_the_judge_tier_dying_and_reports_it(env, corpus, monkeypatch):
    """The deep tier is gone. Deterministic rules still run, findings are still produced, and the
    run is marked degraded instead of quietly returning a thinner result as if it were complete."""
    msa_id = corpus.add("msa.txt", MSA_TEXT, "msa")
    corpus.seed_extract(
        msa_id,
        "msa",
        [
            ("contract.payment_terms.net_days", 60, "net 60 days"),
            ("contract.renewal.type", "auto", "renews automatically"),
            ("contract.renewal.notice_days", 30, "30 days notice"),
        ],
    )

    real_build = __import__("ledgerline.llm.client", fromlist=["build_client"]).build_client

    def failing_judge(settings):
        return FaultyClient(real_build(settings), FaultPlan(fail_stages={"examine_judge"}))

    monkeypatch.setattr("ledgerline.service.build_client", failing_judge)

    outcome = execute_run(corpus.queue_run())

    assert outcome.degraded is True
    with db_module.session_scope() as session:
        run = session.get(Run, outcome.run_id)
        assert "judge" in (run.degraded_reason or "").lower()

    examine = next(entry for entry in outcome.stage_log if entry["stage"] == "examine")
    assert examine["decision"] == "judge_unavailable_deterministic_only"
    assert examine["judge_rules_run"] == 0

    # The deterministic half still did its job.
    with db_module.session_scope() as session:
        rule_ids = {
            item.rule_id
            for item in session.scalars(
                select(PendingItem).where(
                    PendingItem.pile_id == corpus.pile_id, PendingItem.kind == ItemKind.finding
                )
            )
        }
    assert "PAY_TERMS_MAX" in rule_ids
    assert "AUTORENEW_NOTICE" in rule_ids


def test_a_missing_fixture_fails_loudly_rather_than_returning_nothing(env, tmp_path):
    """Replay must never paper over an unrecorded request. A silent empty answer here would look
    exactly like a document that stated nothing."""
    client = ReplayClient(tmp_path)
    with pytest.raises(KeyError) as exc:
        client.invoke(_request(), "some-model")
    assert "Fix:" in str(exc.value)


def test_a_judged_finding_without_a_resolvable_span_says_so(env, corpus):
    msa_id = corpus.add("msa.txt", MSA_TEXT, "msa")
    corpus.seed_extract(msa_id, "msa", [("contract.payment_terms.net_days", 60, "net 60 days")])
    corpus.seed_judge(msa_id, triggered=True, detail="Indemnity binds the vendor only.")

    execute_run(corpus.queue_run())

    with db_module.session_scope() as session:
        findings = session.scalars(
            select(PendingItem).where(
                PendingItem.pile_id == corpus.pile_id,
                PendingItem.kind == ItemKind.finding,
                PendingItem.rule_id == "UNILATERAL_INDEMNITY",
            )
        ).all()

    assert findings, "the judge tier produced no finding"
    assert "rests on judgement alone" in findings[0].detail
    assert findings[0].evidence == []

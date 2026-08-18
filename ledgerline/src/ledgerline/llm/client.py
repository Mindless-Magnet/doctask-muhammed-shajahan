"""One way in and out of every model call in the system.

Four implementations behind one protocol:

  BedrockClient   real calls through the Converse API, structured output via tool use
  RecordingClient wraps a live client and writes each exchange to a fixture file
  ReplayClient    reads fixtures only, never touches the network, used by the whole test suite
  FaultyClient    wraps another client and injects throttles and errors on demand

The test suite runs entirely on ReplayClient, so anyone can verify this system's claims without an
AWS account and without spending money. Fixtures are keyed by a hash of the full request, so a
changed prompt misses the cache loudly rather than silently replaying a stale answer.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ledgerline.config import ModelTier, Settings
from ledgerline.llm.pricing import PRICES_PULLED_AT, cost_usd


class ModelUnavailableError(RuntimeError):
    """Every tier and every fallback failed. The caller degrades; it does not crash."""


class ThrottledError(RuntimeError):
    """Retryable. Bedrock raises this under on-demand capacity pressure."""


class FixtureMissError(KeyError):
    """Replay was asked for a request that was never recorded."""


@dataclass(frozen=True, slots=True)
class ModelRequest:
    tier: ModelTier
    system: str
    user_content: str
    output_schema: dict[str, Any]
    stage: str
    temperature: float = 0.0
    max_tokens: int = 4096

    def cache_key(self, model_id: str) -> str:
        payload = json.dumps(
            {
                "model_id": model_id,
                "system": self.system,
                "user_content": self.user_content,
                "output_schema": self.output_schema,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass(slots=True)
class Usage:
    stage: str
    model_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: float | None
    attempts: int = 1
    degraded_from: str | None = None
    replayed: bool = False


@dataclass(slots=True)
class ModelResponse:
    data: dict[str, Any]
    usage: Usage


class ModelClient(Protocol):
    def invoke(self, request: ModelRequest, model_id: str) -> ModelResponse: ...


# --------------------------------------------------------------------------------------------
# Live
# --------------------------------------------------------------------------------------------


class BedrockClient:
    """Structured output through Converse API tool use. Bedrock has no JSON mode."""

    TOOL_NAME = "emit_result"

    def __init__(self, region: str) -> None:
        import boto3

        self._client = boto3.client("bedrock-runtime", region_name=region)

    def invoke(self, request: ModelRequest, model_id: str) -> ModelResponse:
        from botocore.exceptions import ClientError

        started = time.perf_counter()
        try:
            response = self._client.converse(
                modelId=model_id,
                system=[{"text": request.system}],
                messages=[{"role": "user", "content": [{"text": request.user_content}]}],
                inferenceConfig={
                    "temperature": request.temperature,
                    "maxTokens": request.max_tokens,
                },
                toolConfig={
                    "tools": [
                        {
                            "toolSpec": {
                                "name": self.TOOL_NAME,
                                "description": "Return the result in the required shape.",
                                "inputSchema": {"json": request.output_schema},
                            }
                        }
                    ],
                    "toolChoice": {"tool": {"name": self.TOOL_NAME}},
                },
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceeded"}:
                raise ThrottledError(f"{model_id} throttled: {code}") from exc
            raise ModelUnavailableError(f"{model_id} failed with {code}") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        data = self._first_tool_input(response, model_id)
        usage = response.get("usage", {})
        input_tokens = int(usage.get("inputTokens", 0))
        output_tokens = int(usage.get("outputTokens", 0))
        return ModelResponse(
            data=data,
            usage=Usage(
                stage=request.stage,
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                cost_usd=cost_usd(model_id, input_tokens, output_tokens),
            ),
        )

    def _first_tool_input(self, response: dict[str, Any], model_id: str) -> dict[str, Any]:
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        for block in blocks:
            if "toolUse" in block:
                return dict(block["toolUse"].get("input", {}))
        raise ModelUnavailableError(
            f"{model_id} returned no tool use block. "
            f"Cause: the model answered in prose despite toolChoice being forced. "
            f"Fix: this model does not support forced tool use; move the stage to another tier."
        )


# --------------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------------


def _fixture_path(directory: Path, key: str) -> Path:
    return directory / f"{key}.json"


class RecordingClient:
    """Wraps a live client and writes every exchange to disk. Run once, commit the fixtures."""

    def __init__(self, inner: ModelClient, fixtures_dir: Path) -> None:
        self._inner = inner
        self._dir = fixtures_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def invoke(self, request: ModelRequest, model_id: str) -> ModelResponse:
        response = self._inner.invoke(request, model_id)
        key = request.cache_key(model_id)
        _fixture_path(self._dir, key).write_text(
            json.dumps(
                {
                    "key": key,
                    "model_id": model_id,
                    "stage": request.stage,
                    "recorded_prices_at": PRICES_PULLED_AT,
                    "request_preview": request.user_content[:400],
                    "data": response.data,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "latency_ms": response.usage.latency_ms,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return response


class ReplayClient:
    """Reads committed fixtures. Never touches the network. Counts calls for resume tests."""

    def __init__(self, fixtures_dir: Path) -> None:
        self._dir = fixtures_dir
        self.calls: list[tuple[str, str]] = []

    def invoke(self, request: ModelRequest, model_id: str) -> ModelResponse:
        key = request.cache_key(model_id)
        path = _fixture_path(self._dir, key)
        if not path.exists():
            recorded = len(list(self._dir.glob("*.json"))) if self._dir.exists() else 0
            if recorded == 0:
                raise FixtureMissError(
                    f"No recorded model responses at all, so nothing can be replayed "
                    f"(looked in {self._dir}). "
                    "Cause: this checkout has no committed fixtures, and replay mode never calls a "
                    "live model. "
                    "Fix: either run `make demo-fixtures` once with AWS credentials to record them, "
                    "or set LEDGERLINE_MODEL_CLIENT=bedrock to call the model directly."
                )
            raise FixtureMissError(
                f"No fixture for stage '{request.stage}' key {key}, though {recorded} other "
                f"responses are recorded. "
                "Cause: this exact request differs from anything recorded — a changed prompt, a "
                "changed model, or a document that was not part of the recorded corpus. "
                "Fix: re-record with `make demo-fixtures`, or run against the documents the "
                "fixtures were recorded from."
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.calls.append((request.stage, key))
        input_tokens = int(payload.get("input_tokens", 0))
        output_tokens = int(payload.get("output_tokens", 0))
        return ModelResponse(
            data=payload["data"],
            usage=Usage(
                stage=request.stage,
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=int(payload.get("latency_ms", 0)),
                cost_usd=cost_usd(model_id, input_tokens, output_tokens),
                replayed=True,
            ),
        )


@dataclass
class FaultPlan:
    """Deterministic fault injection.

    Faults can be keyed by stage or by model id. Both exist because they model different real
    failures: a stage-keyed fault is "this kind of work is broken", a model-keyed fault is what
    Bedrock actually does, which is throttle one model while its neighbours are fine. Only the
    model-keyed form can exercise tier fallback, because a stage-keyed throttle follows the request
    down every tier and exhausts them all.
    """

    throttle_stages: dict[str, int] = field(default_factory=dict)
    fail_stages: set[str] = field(default_factory=set)
    throttle_models: dict[str, int] = field(default_factory=dict)
    fail_models: set[str] = field(default_factory=set)


class FaultyClient:
    def __init__(self, inner: ModelClient, plan: FaultPlan) -> None:
        self._inner = inner
        self._plan = plan
        self._seen: dict[str, int] = {}

    def invoke(self, request: ModelRequest, model_id: str) -> ModelResponse:
        stage = request.stage
        if stage in self._plan.fail_stages:
            raise ModelUnavailableError(f"injected failure for stage '{stage}'")
        if model_id in self._plan.fail_models:
            raise ModelUnavailableError(f"injected failure for model '{model_id}'")

        for key, budget in (
            (f"stage:{stage}", self._plan.throttle_stages.get(stage, 0)),
            (f"model:{model_id}", self._plan.throttle_models.get(model_id, 0)),
        ):
            seen = self._seen.get(key, 0)
            if seen < budget:
                self._seen[key] = seen + 1
                raise ThrottledError(f"injected throttle {seen + 1}/{budget} for {key}")

        return self._inner.invoke(request, model_id)


# --------------------------------------------------------------------------------------------
# Tiering, retry and fallback
# --------------------------------------------------------------------------------------------


class TieredModelClient:
    """Resolves a tier to a model, retries throttles with jittered backoff, then falls back a tier.

    When every option is exhausted it raises ModelUnavailableError. The graph catches that and
    continues in deterministic-only mode with the degradation recorded in the run output. It does
    not crash, and it does not pretend the model answered.
    """

    def __init__(self, inner: ModelClient, settings: Settings, sleep=time.sleep) -> None:
        self._inner = inner
        self._settings = settings
        self._sleep = sleep
        self.usages: list[Usage] = []

    def invoke(self, request: ModelRequest) -> ModelResponse:
        chain: list[ModelTier] = [request.tier, *self._settings.model_fallbacks.get(request.tier, [])]  # type: ignore[list-item]
        attempts = 0
        last_error: Exception | None = None

        for position, tier in enumerate(chain):
            model_id = self._settings.model_for_tier(tier)
            for retry in range(self._settings.max_model_retries):
                attempts += 1
                try:
                    response = self._inner.invoke(request, model_id)
                except ThrottledError as exc:
                    last_error = exc
                    delay = self._settings.retry_base_delay_seconds * (2**retry)
                    self._sleep(delay * (0.5 + random.random()))
                    continue
                except ModelUnavailableError as exc:
                    last_error = exc
                    break
                response.usage.attempts = attempts
                if position > 0:
                    response.usage.degraded_from = request.tier
                self.usages.append(response.usage)
                return response

        raise ModelUnavailableError(
            f"stage '{request.stage}' exhausted tier {request.tier} and all fallbacks "
            f"after {attempts} attempts: {last_error}"
        )

    # Reporting -------------------------------------------------------------------------------

    def cost_report(self) -> dict[str, Any]:
        by_stage: dict[str, dict[str, Any]] = {}
        for usage in self.usages:
            bucket = by_stage.setdefault(
                usage.stage,
                {
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                    "unpriced_calls": 0,
                    "latencies_ms": [],
                    "degraded_calls": 0,
                },
            )
            bucket["calls"] += 1
            bucket["input_tokens"] += usage.input_tokens
            bucket["output_tokens"] += usage.output_tokens
            bucket["latencies_ms"].append(usage.latency_ms)
            if usage.cost_usd is None:
                bucket["unpriced_calls"] += 1
            else:
                bucket["cost_usd"] += usage.cost_usd
            if usage.degraded_from:
                bucket["degraded_calls"] += 1

        for bucket in by_stage.values():
            latencies = sorted(bucket.pop("latencies_ms"))
            bucket["latency_ms_p50"] = _percentile(latencies, 0.50)
            bucket["latency_ms_p95"] = _percentile(latencies, 0.95)
            bucket["latency_ms_max"] = latencies[-1] if latencies else 0
            bucket["cost_usd"] = round(bucket["cost_usd"], 6)

        return {
            "prices_pulled_at": PRICES_PULLED_AT,
            "total_calls": len(self.usages),
            "total_cost_usd": round(
                sum(u.cost_usd for u in self.usages if u.cost_usd is not None), 6
            ),
            "unpriced_calls": sum(1 for u in self.usages if u.cost_usd is None),
            "replayed_calls": sum(1 for u in self.usages if u.replayed),
            "by_stage": by_stage,
        }


def _percentile(sorted_values: list[int], fraction: float) -> int:
    if not sorted_values:
        return 0
    index = min(len(sorted_values) - 1, int(round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


def build_client(settings: Settings) -> ModelClient:
    match settings.model_client:
        case "bedrock":
            return BedrockClient(settings.aws_region)
        case "record":
            return RecordingClient(BedrockClient(settings.aws_region), settings.fixtures_dir)
        case "replay":
            return ReplayClient(settings.fixtures_dir)
        case "faulty":
            return FaultyClient(ReplayClient(settings.fixtures_dir), FaultPlan())
        case _:  # pragma: no cover
            raise ValueError(f"unknown model client mode: {settings.model_client}")

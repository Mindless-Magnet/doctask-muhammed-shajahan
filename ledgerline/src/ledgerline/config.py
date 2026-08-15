from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ModelTier = Literal["cheap", "standard", "deep"]


class Settings(BaseSettings):
    """Runtime configuration. Every value has a working default except the database URL.

    Nothing here reads a credential from anywhere but the environment. No key is ever logged;
    see `safe_dump()` for the only representation permitted in output.
    """

    model_config = SettingsConfigDict(env_prefix="LEDGERLINE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ledgerline:ledgerline@localhost:5432/ledgerline"

    # Model access. The client mode decides whether a real call is ever made.
    model_client: Literal["bedrock", "replay", "record", "faulty"] = "replay"
    aws_region: str = "us-east-1"

    model_cheap: str = "us.amazon.nova-lite-v1:0"
    model_standard: str = "us.amazon.nova-pro-v1:0"
    # The judge is deliberately a different model family from the extractor. A verifier that shares
    # the extractor's architecture shares its failure modes and is not an independent check: a
    # correlated hallucination would have to occur twice, in two families, to reach the register.
    model_deep: str = "us.amazon.nova-2-lite-v1:0"

    # Fallback chain used when a tier throttles or errors. Empty means degrade straight to
    # deterministic-only mode.
    model_fallbacks: dict[str, list[str]] = Field(
        default_factory=lambda: {
            # The judge never falls back into the extractor's family. Losing model independence is
            # worse than losing the tier, so the judge degrades to deterministic-only instead and
            # the run is marked degraded rather than quietly verifying itself.
            "deep": [],
            "standard": ["cheap"],
            "cheap": [],
        }
    )

    max_model_retries: int = 3
    retry_base_delay_seconds: float = 1.0

    # Confidence floors. Below these the graph escalates to a human instead of guessing.
    classify_confidence_floor: float = 0.75
    conflict_confidence_floor: float = 0.60

    # Retrieval only engages above this pile size; below it every span is passed directly.
    vector_retrieval_threshold_docs: int = 25

    fixtures_dir: Path = Path("tests/fixtures/llm")
    watch_dir: Path = Path("./inbox")
    storage_dir: Path = Path("./storage")

    playbook_path: Path = Path("src/ledgerline/rules/playbook.yaml")

    worker_poll_seconds: float = 1.0
    worker_claim_batch: int = 1

    def model_for_tier(self, tier: ModelTier) -> str:
        return {"cheap": self.model_cheap, "standard": self.model_standard, "deep": self.model_deep}[
            tier
        ]

    def safe_dump(self) -> dict[str, str]:
        """Representation safe to write to a log. Redacts the database password."""
        url = self.database_url
        if "@" in url and "//" in url:
            head, tail = url.split("//", 1)
            creds, host = tail.split("@", 1)
            user = creds.split(":", 1)[0]
            url = f"{head}//{user}:***@{host}"
        return {
            "database_url": url,
            "model_client": self.model_client,
            "aws_region": self.aws_region,
            "models": f"{self.model_cheap} / {self.model_standard} / {self.model_deep}",
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()

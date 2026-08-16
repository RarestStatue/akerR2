"""Settings, read from the environment / .env."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    postgres_db: str = "aker"
    postgres_user: str = "aker"
    postgres_password: str = "change_me"
    postgres_host: str = "localhost"
    postgres_port: int = 5434

    aker_data_dir: Path = Field(default=REPO_ROOT / "Aker Case Study Data")
    aker_log_level: str = "INFO"

    # --- AI insight layer (optional; the ETL never touches any of this) ---
    ollama_host: str = "http://localhost:11434"
    aker_insight_model: str = "qwen3.5:4b"
    aker_insight_num_ctx_map: int = 4096
    aker_insight_num_ctx_reduce: int = 8192
    aker_insight_jobs: int = 1
    aker_insight_enabled: bool = True
    aker_insight_positioning: bool = True   # third pass: quadrant-movement advice

    @field_validator("aker_insight_model")
    @classmethod
    def _reject_unpinned_tag(cls, v: str) -> str:
        """A bare or :latest tag resolves to the 9B, which does not fit 6 GB of VRAM.

        Weights alone are 6.6 GB, so Ollama silently offloads layers to CPU and a
        23-call run goes from seconds to an afternoon. See PLAN.md 6.6.
        """
        tag = v.strip()
        if ":" not in tag:
            raise ValueError(
                f"AKER_INSIGHT_MODEL={v!r} has no tag. Pin an explicit size "
                f"(e.g. 'qwen3.5:4b') -- a bare name resolves to :latest, which is "
                f"the 9B and does not fit 6 GB of VRAM. See PLAN.md section 6.6."
            )
        if tag.endswith(":latest"):
            raise ValueError(
                f"AKER_INSIGHT_MODEL={v!r} uses the :latest tag. For qwen3.5 that is "
                f"the 9B (6.6 GB of weights) and it will run partly on CPU. Pin "
                f"'qwen3.5:4b'. See PLAN.md section 6.6."
            )
        return tag

    @property
    def dsn(self) -> str:
        return (
            f"host={self.postgres_host} port={self.postgres_port} "
            f"dbname={self.postgres_db} user={self.postgres_user} "
            f"password={self.postgres_password}"
        )

    @property
    def rent_roll_dir(self) -> Path:
        return self.aker_data_dir / "Rent_Roll_with_Lease_Charges"

    @property
    def availability_dir(self) -> Path:
        return self.aker_data_dir / "Unit_Availability"


def get_settings() -> Settings:
    return Settings()

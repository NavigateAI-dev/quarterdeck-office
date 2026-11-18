from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    database_url: SecretStr
    database_password: SecretStr | None = None
    chief_model_alias: str = "0001"
    research_model_alias: str = "0002"
    strategy_model_alias: str = "0003"
    critic_model_alias: str = "0004"
    llm_timeout_seconds: float = Field(default=240, gt=0, le=300)
    llm_max_retries: int = Field(default=2, ge=0, le=3)
    # Thinking shares this cap with the JSON report on models that think by
    # default; 16k leaves room for both on a non-streaming request.
    llm_max_output_tokens: int = Field(default=16000, ge=16, le=16384)
    # Long-form deliverables (strategy/summary writing) need more room than
    # structured role outputs; only those calls use this larger cap.
    deliverable_max_output_tokens: int = Field(default=16384, ge=16, le=65536)
    structured_output_attempts: int = Field(default=3, ge=1, le=4)
    # Default per-run budget. Cost uses gateway-reported prices only.
    run_max_tokens: int = Field(default=400_000, gt=0, le=50_000_000)
    run_max_cost_usd: float | None = Field(default=None, gt=0)
    run_max_minutes: int | None = Field(default=30, gt=0, le=1440)
    # Parallel research subtasks run at most this many at once.
    max_parallel_subtasks: int = Field(default=4, ge=1, le=8)
    # Tasks the worker runs at the same time (MW); the owner may change it.
    max_concurrent_runs: int = Field(default=3, ge=1, le=8)
    # Where the office is published on the Mac. 127.0.0.1 is this computer
    # only; `python3 -m scripts.access home` sets 0.0.0.0 and records the Mac's
    # own network addresses here, for the phone to use.
    office_bind: str = "127.0.0.1"
    office_lan_addresses: str = ""
    # The remote container's exchange folder; empty when it is not set up.
    remote_exchange: Path | None = None
    # Slack conversation: its own daily token allowance, separate from runs.
    chat_daily_max_tokens: int = Field(default=200_000, ge=0, le=5_000_000)
    # Room and chat replies. On models that think by default (Claude Opus 5),
    # thinking counts toward this cap, so a cap sized for the visible reply
    # alone cuts the reply off before it is written.
    chat_max_output_tokens: int = Field(default=4096, ge=64, le=16384)
    max_revision_loops: int = Field(default=3, ge=0, le=10)
    search_provider: str = "static"
    search_base_url: AnyHttpUrl = AnyHttpUrl("https://api.search.brave.com/res/v1/web/search")
    search_api_key: SecretStr | None = None
    # DeepSeek's Search and Fetch APIs: search may use this key
    # when SEARCH_API_KEY is unset; FETCH_FALLBACK=deepseek reads a page through
    # DeepSeek when the office's own reader cannot.
    deepseek_api_key: SecretStr | None = None
    fetch_fallback: str = "none"
    search_timeout_seconds: float = Field(default=20, gt=0, le=120)
    search_max_results: int = Field(default=10, ge=1, le=25)
    enable_page_fetch: bool = True
    # Pages are read by the no-credential fetch service through this exchange.
    # Empty means the office reads them itself, which is for development only.
    fetch_exchange: Path | None = None
    # Where the office asks its own browser, the sandboxed service.
    browser_exchange: Path | None = None
    # When set, only these hosts may be read. The service enforces it.
    fetch_allow_hosts: str = ""
    fetch_max_pages: int = Field(default=5, ge=0, le=15)
    fetch_timeout_seconds: float = Field(default=15, gt=0, le=60)
    office_name: str = Field(default="Quarterdeck", min_length=1, max_length=128)
    # Semantic judgments (TypeSafe Jev) for loop convergence and criterion
    # reachability. Optional: without it the lexical fallback is used.
    typesafe_api_key: SecretStr | None = None
    owner_name: str = Field(default="owner", min_length=1, max_length=255)
    owner_api_token: SecretStr | None = None
    workspace_root: Path = Path("workspace")
    # Encrypted provider/connector credentials. The root key lives in its own
    # private file, outside the database, the store and the workspace volume.
    secret_store_path: Path | None = None
    # Exchange directory shared with the read-only `sources` service.
    sources_exchange: Path = Path("sources-exchange")
    # Where scripts.backup writes archives. Keep it off the workspace volume.
    backup_dir: Path = Path("backups")
    secret_key_file: Path = Path(".secrets/root.key")
    # Engineering clones live apart from the main workspace, because the
    # isolated runner mounts this directory and nothing else.
    engineering_workspace: Path | None = None
    # "isolated" hands repository tests to the runner service; "local" runs
    # them in-process and is for development only.
    engineering_test_runner: str = Field(default="isolated", pattern="^(isolated|local)$")
    # How long the office's own checks of a coding task may run. (The name is
    # from when the office ran Codex itself; Stage 36 moved coding to the Mac.)
    codex_timeout_seconds: float = Field(default=900, gt=0, le=3600)
    # The coding CLI on the owner's Mac, until the owner chooses in Settings.
    coding_agent: str = Field(default="claude_code", pattern="^(claude_code|codex|opencode|dsh)$")
    # Operator-managed engineering target. What may change is configured here,
    # never proposed by a model.
    engineering_repository: Path | None = None
    engineering_base_branch: str = "main"
    engineering_test_command: str | None = None
    engineering_permitted_paths: str = ""
    # Forge publication. ENABLE_GITHUB_PR is the specification's flag name and
    # governs publication whichever forge is configured.
    gitlab_base_url: AnyHttpUrl = AnyHttpUrl("https://gitlab.com")
    gitlab_project_path: str = ""
    gitlab_token: SecretStr | None = None
    forge_timeout_seconds: float = Field(default=30, gt=0, le=120)

    @property
    def default_budget(self) -> dict[str, Any]:
        return {
            "max_tokens": self.run_max_tokens,
            "max_cost_usd": self.run_max_cost_usd,
            "max_minutes": self.run_max_minutes,
        }

    @property
    def engineering_root(self) -> Path:
        return self.engineering_workspace or self.workspace_root / "engineering"

    @property
    def credential_store_path(self) -> Path:
        return self.secret_store_path or self.workspace_root / "secrets" / "credentials.json"

    @property
    def permitted_paths(self) -> list[str]:
        return [
            part.strip() for part in self.engineering_permitted_paths.split(",") if part.strip()
        ]

    enable_code_execution: bool = False
    enable_github_pr: bool = False
    enable_production_deployment: bool = False
    # Coding agents may change files in a space's folders. Needs a
    # writable mount of the approved root, which the owner turns on; off by default.
    workspaces_writable: bool = False
    # Where the approved root is mounted writable inside the office (e.g.
    # /workspaces-rw), when the owner turns work access on. Unset: none.
    workspaces_write_root: Path | None = None

    @model_validator(mode="after")
    def apply_database_password(self) -> "Settings":
        if self.database_password is not None:
            url = make_url(self.database_url.get_secret_value()).set(
                password=self.database_password.get_secret_value()
            )
            self.database_url = SecretStr(url.render_as_string(hide_password=False))
        return self

    @model_validator(mode="after")
    def require_search_credential(self) -> "Settings":
        own_key = self.search_api_key is not None or (
            self.search_provider == "deepseek" and self.deepseek_api_key is not None
        )
        if self.search_provider != "static" and not own_key:
            raise ValueError("SEARCH_API_KEY is required for the selected SEARCH_PROVIDER")
        if self.fetch_fallback == "deepseek" and self.deepseek_api_key is None:
            raise ValueError("DEEPSEEK_API_KEY is required for FETCH_FALLBACK=deepseek")
        return self

    @field_validator(
        "engineering_repository",
        "secret_store_path",
        "engineering_workspace",
        "fetch_exchange",
        "browser_exchange",
        "remote_exchange",
        mode="before",
    )
    @classmethod
    def blank_path_is_unset(cls, value: object) -> object:
        """An empty setting means unset, not the current directory.

        Path("") resolves to ".", so an unconfigured engineering target would
        silently become the application's own source tree.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "engineering_test_command",
        "run_max_cost_usd",
        "run_max_minutes",
        "workspaces_write_root",
        mode="before",
    )
    @classmethod
    def blank_command_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def require_write_root(self) -> "Settings":
        """Work access cannot be on without a writable root to work in (6d)."""
        if self.workspaces_writable and self.workspaces_write_root is None:
            raise ValueError("WORKSPACES_WRITABLE requires WORKSPACES_WRITE_ROOT")
        return self

    @model_validator(mode="after")
    def require_forge_configuration(self) -> "Settings":
        """Publication cannot be enabled without somewhere to publish to."""
        if self.enable_github_pr and not (self.gitlab_project_path and self.gitlab_token):
            raise ValueError("ENABLE_GITHUB_PR requires GITLAB_PROJECT_PATH and GITLAB_TOKEN")
        return self

    @field_validator("gitlab_token")
    @classmethod
    def validate_forge_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        token = value.get_secret_value().strip()
        if not token:
            return None
        if len(token) < 20:
            raise ValueError("GITLAB_TOKEN does not look like a project access token")
        return SecretStr(token)

    @field_validator("owner_api_token")
    @classmethod
    def validate_owner_token(cls, value: SecretStr | None) -> SecretStr | None:
        """A weak owner token is the whole access control for approvals."""
        if value is None:
            return None
        token = value.get_secret_value()
        if len(token) < 32 or "replace" in token.lower() or "change-me" in token.lower():
            raise ValueError("OWNER_API_TOKEN must be a configured value of at least 32 characters")
        return value

    @field_validator("enable_production_deployment")
    @classmethod
    def prohibit_production_deployment(cls, value: bool) -> bool:
        if value:
            raise ValueError("Production deployment is not supported in V0")
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError:
            raise ValueError("DATABASE_URL must be a PostgreSQL connection URL") from None
        if url.drivername != "postgresql+psycopg" or not url.database:
            raise ValueError("DATABASE_URL must use postgresql+psycopg and name a database")
        return value

    @field_validator(
        "chief_model_alias", "research_model_alias", "strategy_model_alias", "critic_model_alias"
    )
    @classmethod
    def validate_alias(cls, value: str) -> str:
        if not value or len(value) > 64 or not all(c.isalnum() or c in "_-" for c in value):
            raise ValueError(
                "Model aliases must contain only letters, numbers, underscores or dashes"
            )
        return value

    @field_validator("search_provider")
    @classmethod
    def validate_search_provider(cls, value: str) -> str:
        if value not in {"static", "brave", "exa", "tavily", "deepseek"}:
            raise ValueError(
                "SEARCH_PROVIDER must be 'static', 'brave', 'exa', 'tavily' or 'deepseek'"
            )
        return value

    @field_validator("fetch_fallback")
    @classmethod
    def validate_fetch_fallback(cls, value: str) -> str:
        if value not in {"none", "deepseek"}:
            raise ValueError("FETCH_FALLBACK must be 'none' or 'deepseek'")
        return value

    @property
    def model_aliases(self) -> dict[str, str]:
        return {
            "chief": self.chief_model_alias,
            "researcher": self.research_model_alias,
            "strategist": self.strategy_model_alias,
            "critic": self.critic_model_alias,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()

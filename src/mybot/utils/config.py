"""Configuration management with hot reload support."""

import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class LLMConfig(BaseModel):
    """LLM provider configuration."""

    provider: str
    model: str
    api_key: SecretStr
    api_base: str | None = None
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, gt=0)

    @field_validator("api_base")
    @classmethod
    def api_base_must_be_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("api_base must be a valid URL")
        return v


class TelegramConfig(BaseModel):
    """Telegram platform configuration."""

    enabled: bool = True
    bot_token: SecretStr
    allowed_user_ids: list[str] = Field(default_factory=list)


class DiscordConfig(BaseModel):
    """Discord platform configuration."""

    enabled: bool = True
    bot_token: SecretStr
    channel_id: str | None = None
    allowed_user_ids: list[str] = Field(default_factory=list)


class BraveWebSearchConfig(BaseModel):
    """Configuration for web search provider."""

    provider: Literal["brave"] = "brave"
    api_key: SecretStr


class Crawl4AIWebReadConfig(BaseModel):
    """Configuration for web read provider."""

    provider: Literal["crawl4ai"] = "crawl4ai"


class SourceSessionConfig(BaseModel):
    """Session affinity configuration for a source."""

    session_id: str


class ChannelConfig(BaseModel):
    """Channel configuration."""

    enabled: bool = False
    telegram: TelegramConfig | None = None
    discord: DiscordConfig | None = None


class ApiConfig(BaseModel):
    """HTTP API configuration."""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, gt=0, lt=65536)


class ResearchEmbeddingConfig(BaseModel):
    provider: Literal["hashing", "sentence_transformers"] = "hashing"
    model: str = "BAAI/bge-m3"
    revision: str | None = None
    device: str = "auto"
    batch_size: int = Field(default=32, gt=0, le=1024)
    normalize: bool = True
    dimensions: int = Field(default=384, gt=0)
    cache_folder: Path | None = None
    local_files_only: bool = False


class ResearchVectorStoreConfig(BaseModel):
    provider: Literal["in_memory", "qdrant"] = "in_memory"
    mode: Literal["local", "remote"] = "local"
    path: Path = Path(".research/qdrant")
    url: str | None = None
    api_key: SecretStr | None = None
    collection_prefix: str = "research_chunks"

    @model_validator(mode="after")
    def validate_location(self) -> "ResearchVectorStoreConfig":
        if self.provider == "qdrant" and self.mode == "remote" and not self.url:
            raise ValueError("research.vector_store.url is required in remote mode")
        if self.url and not self.url.startswith(("http://", "https://")):
            raise ValueError("research.vector_store.url must be a valid URL")
        return self


class ResearchMetadataStoreConfig(BaseModel):
    url: str = "sqlite:///.research/research.db"

    @field_validator("url")
    @classmethod
    def must_be_sqlite(cls, value: str) -> str:
        if not value.startswith("sqlite:///"):
            raise ValueError("research.metadata_store.url must use sqlite:///")
        return value

    def path(self, workspace: Path) -> Path:
        value = Path(self.url.removeprefix("sqlite:///"))
        return value if value.is_absolute() else workspace / value


class ResearchRetrievalConfig(BaseModel):
    candidate_k: int = Field(default=30, ge=1, le=200)
    top_k: int = Field(default=8, ge=1, le=30)
    max_per_source: int = Field(default=2, ge=1, le=10)
    min_score: float | None = Field(default=None, ge=-1.0, le=1.0)
    reranker: str | None = None


class ResearchMemoryConfig(BaseModel):
    enabled: bool = True
    max_related_runs: int = Field(default=5, ge=0, le=20)
    default_ttl_hours: int = Field(default=168, gt=0)
    official_ttl_hours: int = Field(default=720, gt=0)
    volatile_ttl_hours: int = Field(default=6, gt=0)


class ResearchCitationEvaluationConfig(BaseModel):
    enabled: bool = True
    verifier: str = "rules"
    min_coverage: float = Field(default=0.85, ge=0.0, le=1.0)
    min_support_rate: float = Field(default=0.80, ge=0.0, le=1.0)
    max_repairs: int = Field(default=1, ge=0, le=1)


class ResearchTelemetryConfig(BaseModel):
    enabled: bool = True
    service_name: str = "mybot-research"
    otlp_endpoint: str | None = None
    prometheus_port: int | None = Field(default=None, gt=0, lt=65536)


class ResearchConfig(BaseModel):
    embedding: ResearchEmbeddingConfig = Field(default_factory=ResearchEmbeddingConfig)
    vector_store: ResearchVectorStoreConfig = Field(
        default_factory=ResearchVectorStoreConfig
    )
    metadata_store: ResearchMetadataStoreConfig = Field(
        default_factory=ResearchMetadataStoreConfig
    )
    retrieval: ResearchRetrievalConfig = Field(default_factory=ResearchRetrievalConfig)
    memory: ResearchMemoryConfig = Field(default_factory=ResearchMemoryConfig)
    citation_evaluation: ResearchCitationEvaluationConfig = Field(
        default_factory=ResearchCitationEvaluationConfig
    )
    telemetry: ResearchTelemetryConfig = Field(default_factory=ResearchTelemetryConfig)


class Config(BaseModel):
    """Main configuration with hot reload support."""

    workspace: Path
    llm: LLMConfig
    default_agent: str
    agents_path: Path = Field(default=Path("agents"))
    skills_path: Path = Field(default=Path("skills"))
    crons_path: Path = Field(default=Path("crons"))
    memories_path: Path = Field(default=Path("memories"))
    logging_path: Path = Field(default=Path(".logs"))
    history_path: Path = Field(default=Path(".history"))
    event_path: Path = Field(default=Path(".event"))
    websearch: BraveWebSearchConfig | None = None
    webread: Crawl4AIWebReadConfig | None = None
    channels: ChannelConfig = Field(default_factory=ChannelConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    sources: dict[str, SourceSessionConfig] = Field(default_factory=dict)
    routing: dict = Field(default_factory=lambda: {"bindings": []})
    default_delivery_source: str | None = None

    @model_validator(mode="after")
    def resolve_paths(self) -> "Config":
        """Resolve relative paths to absolute using workspace."""
        for field_name in (
            "agents_path",
            "skills_path",
            "crons_path",
            "memories_path",
            "logging_path",
            "history_path",
            "event_path",
        ):
            path = getattr(self, field_name)
            if not path.is_absolute():
                setattr(self, field_name, self.workspace / path)
        vector_path = self.research.vector_store.path
        if not vector_path.is_absolute():
            self.research.vector_store.path = self.workspace / vector_path
        cache_folder = self.research.embedding.cache_folder
        if cache_folder is not None and not cache_folder.is_absolute():
            self.research.embedding.cache_folder = self.workspace / cache_folder
        return self

    @classmethod
    def load(cls, workspace_dir: Path) -> "Config":
        """Load configuration from workspace directory."""
        config_data = cls._load_merged_configs(workspace_dir)
        config_data["workspace"] = workspace_dir
        return cls.model_validate(config_data)

    @classmethod
    def _load_merged_configs(cls, workspace_dir: Path) -> dict[str, Any]:
        """Load and merge user and runtime config files."""
        config_data: dict[str, Any] = {}

        user_config = workspace_dir / "config.user.yaml"
        runtime_config = workspace_dir / "config.runtime.yaml"

        if user_config.exists():
            with open(user_config) as f:
                config_data = cls._deep_merge(config_data, yaml.safe_load(f) or {})

        if runtime_config.exists():
            with open(runtime_config) as f:
                config_data = cls._deep_merge(config_data, yaml.safe_load(f) or {})

        return cls._resolve_environment(config_data)

    @classmethod
    def _resolve_environment(cls, value: Any) -> Any:
        """Resolve exact ${NAME} placeholders without exposing secret values."""
        if isinstance(value, dict):
            return {key: cls._resolve_environment(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._resolve_environment(item) for item in value]
        if not isinstance(value, str):
            return value

        match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
        if not match:
            return value
        variable_name = match.group(1)
        if variable_name not in os.environ:
            raise ValueError(
                f"required environment variable is not set: {variable_name}"
            )
        return os.environ[variable_name]

    @staticmethod
    def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """Deep merge override dict into base dict."""
        result = base.copy()

        for key, value in override.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(value, dict)
            ):
                result[key] = Config._deep_merge(result[key], value)
            else:
                result[key] = value

        return result

    def _set_nested(self, obj: dict, key: str, value: Any) -> None:
        """Set a nested value in a dict using dot notation."""
        keys = key.split(".")
        for k in keys[:-1]:
            if k not in obj or not isinstance(obj[k], dict):
                obj[k] = {}
            obj = obj[k]
        obj[keys[-1]] = value

    def _set_config_value(self, config_path: Path, key: str, value: Any) -> None:
        """Update a config value in a YAML file."""
        # Load existing or start fresh
        if config_path.exists():
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}

        if isinstance(value, BaseModel):
            value = value.model_dump()

        # Update the key (supports nested via dot notation)
        self._set_nested(data, key, value)

        # Write back
        with open(config_path, "w") as f:
            yaml.dump(data, f)

    def set_user(self, key: str, value: Any) -> None:
        """Update a config value in config.user.yaml."""
        self._set_config_value(self.workspace / "config.user.yaml", key, value)

    def set_runtime(self, key: str, value: Any) -> None:
        """Update a runtime value in config.runtime.yaml."""
        self._set_config_value(self.workspace / "config.runtime.yaml", key, value)

    def reload(self) -> bool:
        """Re-read config.user.yaml and merge with runtime."""
        try:
            config_data = self._load_merged_configs(self.workspace)
            config_data["workspace"] = self.workspace

            # Create new instance and copy values
            new_config = Config.model_validate(config_data)

            # Update all fields from new config
            for field_name in Config.model_fields:
                setattr(self, field_name, getattr(new_config, field_name))

            return True
        except Exception as e:
            logging.debug("Config reload failed: %s", e)
            return False


class ConfigHandler(FileSystemEventHandler):
    """Handles config file modification events."""

    def __init__(self, config: Config):
        self._config = config

    def on_modified(self, event):
        """Reload config when config.user.yaml changes."""
        if not event.is_directory and event.src_path.endswith("config.user.yaml"):
            self._config.reload()


class ConfigReloader:
    """Manages watchdog observer for config hot reload."""

    def __init__(self, config: Config):
        self._config = config
        self._observer = Observer()

    def start(self) -> None:
        """Start watching config file for changes."""
        handler = ConfigHandler(self._config)
        self._observer.schedule(handler, str(self._config.workspace), recursive=False)
        self._observer.start()

    def stop(self) -> None:
        """Stop watching."""
        self._observer.stop()
        self._observer.join()
        del self._observer

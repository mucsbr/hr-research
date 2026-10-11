from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    app_name: str = "观势 · 企业情报"
    app_env: str = "development"
    app_url: str = "http://127.0.0.1:5173"
    database_url: SecretStr = SecretStr("")
    test_database_url: SecretStr = SecretStr("")
    redis_url: SecretStr = SecretStr("")
    app_secret: SecretStr = SecretStr("")
    encryption_key: SecretStr = SecretStr("")
    admin_username: str = "admin"
    admin_password: SecretStr = SecretStr("")
    storage_dir: Path = ROOT / ".runtime" / "artifacts"
    browser_profiles_dir: Path = ROOT / ".runtime" / "browser-profiles"
    browser_cache_dir: Path = ROOT / ".runtime" / "browsers"
    browser_service_url: str = "http://127.0.0.1:8000"
    browser_login_seconds: int = 900
    probe_public_url: str = ""
    probe_ca_file: Path | None = None
    probe_downloads_dir: Path = ROOT / ".runtime" / "probe-downloads"
    probe_online_seconds: int = 60
    probe_command_timeout_seconds: int = 35
    probe_pairing_seconds: int = 600
    allowed_fetch_hosts: str = ""
    allow_local_fixture_fetch: bool = False
    public_dns_fallback: bool = False
    public_dns_resolver: Literal["alidns", "google"] = "alidns"
    company_concurrency: int = 3
    discovery_internal_search_url: str = ""
    discovery_search_timeout_seconds: int = 15
    task_max_pages: int = 20
    task_max_details: int = 100
    task_timeout_seconds: int = 600
    checkpoint_max_age_seconds: int = 3600
    checkpoint_max_raw_bytes: int = 30000000
    checkpoint_max_steps: int = 600
    checkpoint_max_records: int = 500
    adapter_runner_url: str = ""
    adapter_runner_token: SecretStr = SecretStr("")
    adapter_runner_timeout_seconds: int = 10
    adapter_runner_runtime_digest: str = ""
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "glm-5.3"
    llm_reasoning_effort: Literal["low", "high", "max"] = "low"
    llm_fallback_model: str = "deepseek-flash"
    llm_daily_request_limit: int = 12
    llm_task_call_limit: int = 3
    llm_max_output_tokens: int = 1200
    llm_max_input_chars: int = 16000
    llm_timeout_seconds: int = 90
    session_hours: int = 12
    cookie_secure: bool = False
    bettafish_url: str = ""
    bettafish_repo_dir: str = ""
    bettafish_python: str = ""
    bettafish_revision: str = "b67928ff1ce515dd7672408221fb1e9500d0972d"
    bettafish_license_confirmed: bool = False
    bettafish_timeout_seconds: int = 600
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    smtp_security: Literal["starttls", "ssl"] = "starttls"
    notification_allowed_hosts: str = ""
    notification_timeout_seconds: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()

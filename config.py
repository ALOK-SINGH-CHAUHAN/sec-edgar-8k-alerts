"""Configuration management using Pydantic and YAML."""

import os
import re
from pathlib import Path
from typing import List, Literal, Optional, Set
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator
import yaml

# Load .env if present
load_dotenv()

PLACEHOLDER_USER_AGENTS: Set[str] = {
    "yourname your@email.com",
    "your_name your_email@example.com",
    "admincontact@<sample company domain>.com",
    "user@example.com",
    "sample@example.com",
    "test@test.com",
    "firstname lastname email@domain.com",
}


class PollConfig(BaseModel):
    fast_interval_seconds: int = Field(default=10, ge=1, le=3600)
    slow_interval_seconds: int = Field(default=60, ge=5, le=86400)
    fast_window_start: str = Field(default="04:00", description="HH:MM in ET")
    fast_window_end: str = Field(default="20:00", description="HH:MM in ET")
    timezone: str = Field(default="America/New_York")
    holidays: List[str] = Field(default_factory=list, description="List of YYYY-MM-DD market holidays")


class SECConfig(BaseModel):
    user_agent: str = Field(..., min_length=5, description="Descriptive User-Agent: Name and Email")
    max_requests_per_second: float = Field(default=8.0, ge=0.5, le=10.0)
    ticker_map_refresh_hours: int = Field(default=24, ge=1)
    atom_feed_enabled: bool = Field(default=False)
    cooldown_seconds_on_403: int = Field(default=300, ge=1)

    @field_validator("user_agent")
    @classmethod
    def validate_user_agent(cls, v: str) -> str:
        cleaned = v.strip()
        lower = cleaned.lower()
        if not cleaned:
            raise ValueError("SEC user_agent cannot be empty.")
        
        # Check against known placeholders
        for placeholder in PLACEHOLDER_USER_AGENTS:
            if placeholder in lower:
                raise ValueError(
                    f"SEC user_agent contains placeholder '{placeholder}'. "
                    "SEC fair-access rules require a genuine, descriptive User-Agent "
                    "with your name/app and valid contact email (e.g. 'MyApp/1.0 (john.doe@gmail.com)')."
                )
        
        if "<" in cleaned or ">" in cleaned or "your@" in lower or "@email.com" in lower:
            raise ValueError(
                f"SEC user_agent '{cleaned}' appears to be a template or placeholder. "
                "Please configure a valid email/name."
            )

        # SEC requires an email or URL in User-Agent header
        if "@" not in cleaned and "http" not in lower:
            raise ValueError(
                f"SEC user_agent '{cleaned}' must include a valid contact email address or URL."
            )

        return cleaned

    @field_validator("max_requests_per_second")
    @classmethod
    def validate_rate_limit(cls, v: float) -> float:
        if v > 10.0:
            raise ValueError("SEC fair-access policy hard cap is 10 requests/second. Value cannot exceed 10.0.")
        return v


class QuietHoursConfig(BaseModel):
    enabled: bool = Field(default=False)
    start: str = Field(default="22:00", description="HH:MM")
    end: str = Field(default="08:00", description="HH:MM")
    timezone: str = Field(default="Asia/Kolkata")
    mode: Literal["suppress", "queue"] = Field(default="queue")
    high_priority_bypasses: bool = Field(default=True)


class DocumentPreviewConfig(BaseModel):
    enabled: bool = Field(default=False)
    max_chars: int = Field(default=500, ge=50, le=4000)
    timeout_seconds: float = Field(default=3.0, ge=0.5, le=10.0)


class TelegramConfig(BaseModel):
    bot_token: Optional[str] = Field(default=None)
    chat_id: Optional[str] = Field(default=None)

    @model_validator(mode="after")
    def populate_from_env(self) -> "TelegramConfig":
        if not self.bot_token:
            self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not self.chat_id:
            self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        return self


class EmailConfig(BaseModel):
    enabled: bool = Field(default=False)
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_user: str = Field(default="")
    smtp_password: Optional[str] = Field(default=None)
    to_email: str = Field(default="")
    from_email: str = Field(default="")

    @model_validator(mode="after")
    def populate_from_env(self) -> "EmailConfig":
        if not self.smtp_password:
            self.smtp_password = os.getenv("SMTP_PASSWORD")
        return self


class AlertsConfig(BaseModel):
    channels: List[Literal["console", "telegram", "email"]] = Field(default_factory=lambda: ["console"])
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)


class AppConfig(BaseModel):
    watchlist: List[str] = Field(default_factory=list)
    poll: PollConfig = Field(default_factory=PollConfig)
    forms: List[str] = Field(default_factory=lambda: ["8-K", "8-K/A"])
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    sec: SECConfig
    quiet_hours: QuietHoursConfig = Field(default_factory=QuietHoursConfig)
    document_preview: DocumentPreviewConfig = Field(default_factory=DocumentPreviewConfig)
    database_path: str = Field(default="data/edgar_alerts.db")
    log_level: str = Field(default="INFO")

    @field_validator("watchlist")
    @classmethod
    def normalize_watchlist(cls, tickers: List[str]) -> List[str]:
        # Normalize uppercase and strip
        seen = set()
        cleaned: List[str] = []
        for t in tickers:
            symbol = t.strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                cleaned.append(symbol)
        return cleaned


def load_config(config_path: str | Path = "config.yaml") -> AppConfig:
    """Load and validate configuration from YAML file and environment variables."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    # SEC User-Agent can also be overridden from environment variable SEC_USER_AGENT
    env_ua = os.getenv("SEC_USER_AGENT")
    if env_ua:
        if "sec" not in data:
            data["sec"] = {}
        data["sec"]["user_agent"] = env_ua

    return AppConfig.model_validate(data)

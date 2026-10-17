"""Tests for config validation and loading."""

import os
import pytest
from pydantic import ValidationError
from config import AppConfig, SECConfig, load_config


def test_valid_sec_config():
    cfg = SECConfig(user_agent="MyStockAlerts/1.0 (john.trader@gmail.com)")
    assert cfg.user_agent == "MyStockAlerts/1.0 (john.trader@gmail.com)"
    assert cfg.max_requests_per_second == 8.0


def test_placeholder_user_agent_rejected():
    with pytest.raises(ValidationError, match="placeholder"):
        SECConfig(user_agent="YourName your@email.com")

    with pytest.raises(ValidationError, match="placeholder"):
        SECConfig(user_agent="SampleName user@example.com")


def test_missing_contact_in_user_agent():
    with pytest.raises(ValidationError, match="contact email"):
        SECConfig(user_agent="JustMyAppName")


def test_rate_limit_hard_cap():
    # <= 10.0 is allowed
    cfg = SECConfig(user_agent="ValidBot/1.0 (trader@example.org)", max_requests_per_second=10.0)
    assert cfg.max_requests_per_second == 10.0

    # > 10.0 is rejected per SEC fair access rules
    with pytest.raises(ValidationError):
        SECConfig(user_agent="ValidBot/1.0 (trader@example.org)", max_requests_per_second=10.1)


def test_watchlist_normalization():
    cfg = AppConfig(
        watchlist=["aapl", "AAPL", " msft ", "brk-b"],
        sec={"user_agent": "App/1.0 (contact@domain.com)"},
    )
    assert cfg.watchlist == ["AAPL", "MSFT", "BRK-B"]


def test_telegram_env_var_override(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mock_token_123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999888777")
    cfg = AppConfig(
        sec={"user_agent": "App/1.0 (contact@domain.com)"},
    )
    assert cfg.alerts.telegram.bot_token == "mock_token_123"
    assert cfg.alerts.telegram.chat_id == "999888777"

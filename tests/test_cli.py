"""Tests for CLI operations, config manipulation, and backfill."""

from pathlib import Path
import pytest
from ruamel.yaml import YAML
import respx
import httpx

from main import cmd_add_ticker, cmd_remove_ticker, cmd_test_alert
from edgar_client import SEC_TICKERS_URL


@pytest.fixture
def temp_config_yaml(tmp_path) -> Path:
    cfg_path = tmp_path / "config.yaml"
    content = """# Header comment
watchlist:
  - AAPL
  - MSFT

# SEC settings
sec:
  user_agent: "TestApp/1.0 (tester@domain.com)"
  max_requests_per_second: 8.0

alerts:
  channels:
    - console

database_path: "{db_path}"
""".format(db_path=tmp_path / "cli_test.db")

    cfg_path.write_text(content, encoding="utf-8")
    return cfg_path


@pytest.mark.asyncio
@respx.mock
async def test_cli_add_and_remove_ticker(temp_config_yaml, sample_company_tickers_json):
    respx.get(SEC_TICKERS_URL).respond(
        status_code=200, json=sample_company_tickers_json
    )
    # Mock submissions for NVDA silent seed
    respx.get("https://data.sec.gov/submissions/CIK0001045810.json").respond(
        status_code=200, json={"filings": {"recent": {}}}
    )

    # 1. Add NVDA
    exit_code = await cmd_add_ticker("NVDA", str(temp_config_yaml))
    assert exit_code == 0

    # Read back with ruamel.yaml and check comments & watchlist
    yaml = YAML()
    with open(temp_config_yaml, "r", encoding="utf-8") as f:
        data = yaml.load(f)
    assert "NVDA" in data["watchlist"]

    # Verify original comment preserved in raw file
    raw_text = temp_config_yaml.read_text(encoding="utf-8")
    assert "# Header comment" in raw_text

    # 2. Remove NVDA
    exit_code_rem = await cmd_remove_ticker("NVDA", str(temp_config_yaml))
    assert exit_code_rem == 0

    with open(temp_config_yaml, "r", encoding="utf-8") as f:
        data2 = yaml.load(f)
    assert "NVDA" not in data2["watchlist"]
    assert "AAPL" in data2["watchlist"]


@pytest.mark.asyncio
async def test_cli_test_alert(temp_config_yaml):
    exit_code = await cmd_test_alert(str(temp_config_yaml))
    assert exit_code == 0

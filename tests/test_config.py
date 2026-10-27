import json

import pytest

from accounts_api.config import load_settings, normalize_db_url


def test_missing_required_variable_names_it(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        load_settings()
    line = json.loads(capsys.readouterr().err.strip().splitlines()[0])
    assert line["variable"] == "DATABASE_URL"
    assert line["msg"] == "configuration error: environment variable 'DATABASE_URL' is required but not set"


def test_invalid_variable_names_it(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/bank")
    monkeypatch.setenv("DAILY_TRANSFER_LIMIT_CENTS", "ten thousand")
    with pytest.raises(SystemExit):
        load_settings()
    line = json.loads(capsys.readouterr().err.strip().splitlines()[0])
    assert line["variable"] == "DAILY_TRANSFER_LIMIT_CENTS"
    assert "is invalid" in line["msg"]


@pytest.mark.parametrize("url", ["postgresql://u:p@db:5432/bank", "postgres://u:p@db:5432/bank"])
def test_plain_postgres_urls_get_the_driver_name(url):
    assert normalize_db_url(url) == "postgresql+psycopg://u:p@db:5432/bank"

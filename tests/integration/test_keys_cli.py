import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from typer.testing import CliRunner

from memfxxk.auth import authenticate, hash_key
from memfxxk.cli import app
from memfxxk.db import api_keys
from memfxxk.domain import Principal

pytestmark = pytest.mark.integration


def test_create_key_prints_once_and_stores_hash(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMFXXK_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    result = CliRunner().invoke(
        app, ["keys", "create", "--org", "test-org", "--role", "host", "--name", "cli"]
    )

    assert result.exit_code == 0, result.output
    [raw_key] = result.stdout.splitlines()
    assert authenticate(raw_key, engine=engine) == Principal(org_id="test-org", role="host")
    with engine.connect() as connection:
        assert connection.execute(select(api_keys.c.key_hash)).scalar_one() == hash_key(raw_key)

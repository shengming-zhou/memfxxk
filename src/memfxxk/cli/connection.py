from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import typer
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from memfxxk.domain import Actor

if TYPE_CHECKING:
    from memfxxk.client import MemfxxkClient


class ClientSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEMFXXK_", env_file=".env", extra="ignore")

    url: str = "http://127.0.0.1:8000"
    api_key: SecretStr | None = None
    user: str | None = None
    agent: str | None = None
    project: str | None = None

    def connect(self, *, as_user: bool = False) -> "MemfxxkClient":
        from memfxxk.client import MemfxxkClient

        if self.api_key is None or not self.api_key.get_secret_value():
            raise typer.BadParameter("Set MEMFXXK_API_KEY")
        actor = Actor(type="user", id=self.user) if as_user and self.user else None
        return MemfxxkClient(
            self.url,
            self.api_key.get_secret_value(),
            user_id=self.user,
            agent_id=self.agent,
            project_id=self.project,
            actor=actor,
        )


@contextmanager
def open_client(ctx: typer.Context) -> Iterator["MemfxxkClient"]:
    from pydantic import ValidationError

    from memfxxk.client import ClientError

    try:
        with ctx.ensure_object(ClientSettings).connect(as_user=True) as client:
            yield client
    except (ClientError, ValidationError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

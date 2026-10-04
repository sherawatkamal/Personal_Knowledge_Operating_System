"""Typed settings from the environment.

Every credential is a SecretStr, so it never appears in repr() or str(), and every
secret value is registered with the log scrubber as soon as settings load.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pkos import logs

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PKOS_", extra="ignore")

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "pkos"
    db_user: str = "pkos"
    db_password: SecretStr = SecretStr("pkos-local-dev")

    granola_api_key: SecretStr | None = None
    purge_after_days: int = 30  # tombstones older than this are eligible for `pkos purge`

    migrations_dir: Path = REPO_ROOT / "migrations"
    log_level: str = "INFO"

    @field_validator("granola_api_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        """`KEY=` in .env means the source is disabled, not an empty credential."""
        return None if isinstance(v, str) and not v.strip() else v

    def model_post_init(self, __context: object) -> None:
        for value in self.__dict__.values():
            if isinstance(value, SecretStr):
                logs.register_secret(value.get_secret_value())

    def conninfo(self, dbname: str | None = None) -> dict[str, object]:
        """Connection kwargs for psycopg. Never log or print the result."""
        return {
            "host": self.db_host,
            "port": self.db_port,
            "dbname": dbname or self.db_name,
            "user": self.db_user,
            "password": self.db_password.get_secret_value(),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""System configurations: one TOML file per configuration (A2). Ablations are files, not code."""

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pkos.config import REPO_ROOT

CONFIG_DIR = REPO_ROOT / "configs" / "systems"
KINDS = ("null", "oracle", "retrieval")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SystemConfig:
    name: str
    description: str
    kind: str
    diagnostic: bool  # harness self-checks; never publishable
    sha256: str
    raw: dict[str, Any]


def load_config(path: Path) -> SystemConfig:
    data = path.read_bytes()
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name}: {e}") from None
    name = doc.get("name")
    if name != path.stem:
        raise ConfigError(f"{path.name}: name {name!r} must equal the file name {path.stem!r}")
    kind = doc.get("system", {}).get("kind")
    if kind not in KINDS:
        raise ConfigError(f"{path.name}: [system] kind {kind!r} not in {KINDS}")
    return SystemConfig(
        name=name,
        description=doc.get("description", ""),
        kind=kind,
        diagnostic=bool(doc.get("diagnostic", False)),
        sha256=hashlib.sha256(data).hexdigest(),
        raw=doc,
    )


def available(directory: Path = CONFIG_DIR) -> dict[str, SystemConfig]:
    return {p.stem: load_config(p) for p in sorted(directory.glob("*.toml"))}


def resolve(names: list[str], directory: Path = CONFIG_DIR) -> list[SystemConfig]:
    configs = available(directory)
    unknown = [n for n in names if n not in configs]
    if unknown:
        raise ConfigError(
            f"unknown config(s): {', '.join(unknown)} (available: {', '.join(configs) or 'none'})"
        )
    return [configs[n] for n in names]

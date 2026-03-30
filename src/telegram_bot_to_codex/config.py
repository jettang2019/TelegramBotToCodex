from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


class ConfigError(ValueError):
    """Raised when the runtime config is invalid."""


VALID_CODEX_EXECUTION_MODES = {
    "default",
    "full-auto",
    "danger-full-access",
}

VALID_REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
}


def normalize_username(username: str) -> str:
    normalized = username.strip().lower().removeprefix("@")
    if not normalized:
        raise ConfigError("telegram_username must not be empty")
    return normalized


@dataclass(frozen=True)
class AppSettings:
    codex_bin: str
    state_path: Path
    poll_timeout_seconds: int
    log_level: str


@dataclass(frozen=True)
class BotSettings:
    name: str
    token: str
    workdir: Path
    telegram_username: str
    telegram_user_id: Optional[int]
    skip_git_repo_check: bool
    codex_execution_mode: str
    model: Optional[str]
    effort: Optional[str]

    @property
    def normalized_username(self) -> str:
        return normalize_username(self.telegram_username)


@dataclass(frozen=True)
class ServiceConfig:
    app: AppSettings
    bots: Tuple[BotSettings, ...]


def load_config(path: Path) -> ServiceConfig:
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}. Copy config.example.toml to config.toml first."
        )

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    app_section = _require_table(raw, "app")
    bots_section = raw.get("bots")

    if not isinstance(bots_section, list) or not bots_section:
        raise ConfigError("Config must contain at least one [[bots]] entry")

    config_dir = path.parent.resolve()
    app = AppSettings(
        codex_bin=_require_string(app_section, "codex_bin", default="codex"),
        state_path=_resolve_path(
            config_dir,
            _require_string(app_section, "state_path", default=".local/state.json"),
        ),
        poll_timeout_seconds=_require_int(app_section, "poll_timeout_seconds", default=30),
        log_level=_require_string(app_section, "log_level", default="INFO").upper(),
    )

    if app.poll_timeout_seconds < 1:
        raise ConfigError("app.poll_timeout_seconds must be >= 1")

    bots = tuple(_parse_bot(entry, config_dir) for entry in bots_section)
    _validate_unique_bot_names(bots)
    return ServiceConfig(app=app, bots=bots)


def _parse_bot(raw: Dict[str, Any], config_dir: Path) -> BotSettings:
    name = _require_string(raw, "name")
    token = _require_string(raw, "token")
    workdir = _resolve_path(config_dir, _require_string(raw, "workdir"))
    telegram_username = _require_string(raw, "telegram_username")
    telegram_user_id = _require_optional_int(raw, "telegram_user_id")
    skip_git_repo_check = _require_bool(raw, "skip_git_repo_check", default=True)
    codex_execution_mode = _require_string(raw, "codex_execution_mode", default="full-auto")
    model = _require_optional_string(raw, "model")
    effort = _require_optional_string(raw, "effort")

    if not workdir.is_dir():
        raise ConfigError(f"Bot '{name}' workdir does not exist or is not a directory: {workdir}")

    normalize_username(telegram_username)
    if codex_execution_mode not in VALID_CODEX_EXECUTION_MODES:
        raise ConfigError(
            "Config key 'codex_execution_mode' must be one of: "
            + ", ".join(sorted(VALID_CODEX_EXECUTION_MODES))
        )
    if effort is not None and effort not in VALID_REASONING_EFFORTS:
        raise ConfigError(
            "Config key 'effort' must be one of: "
            + ", ".join(sorted(VALID_REASONING_EFFORTS))
        )

    return BotSettings(
        name=name,
        token=token,
        workdir=workdir,
        telegram_username=telegram_username,
        telegram_user_id=telegram_user_id,
        skip_git_repo_check=skip_git_repo_check,
        codex_execution_mode=codex_execution_mode,
        model=model,
        effort=effort,
    )


def _validate_unique_bot_names(bots: Tuple[BotSettings, ...]) -> None:
    names = set()
    for bot in bots:
        if bot.name in names:
            raise ConfigError(f"Duplicate bot name: {bot.name}")
        names.add(bot.name)


def _resolve_path(config_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    return path.resolve()


def _require_table(raw: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"Missing [{key}] section")
    return value


def _require_string(raw: Dict[str, Any], key: str, default: Optional[str] = None) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Config key '{key}' must be a non-empty string")
    return value


def _require_int(raw: Dict[str, Any], key: str, default: Optional[int] = None) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"Config key '{key}' must be an integer")
    return value


def _require_optional_int(raw: Dict[str, Any], key: str) -> Optional[int]:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"Config key '{key}' must be an integer when provided")
    return value


def _require_optional_string(raw: Dict[str, Any], key: str) -> Optional[str]:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Config key '{key}' must be a non-empty string when provided")
    return value


def _require_bool(raw: Dict[str, Any], key: str, default: Optional[bool] = None) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"Config key '{key}' must be a boolean")
    return value

"""Manage .env file for secrets and configuration."""

import os
import secrets
from pathlib import Path

ENV_PATH = Path(__file__).parent.parent / ".env"

# Keys that are considered secrets (masked in API responses)
SECRET_KEYS = {"JIRA_API_TOKEN", "BITBUCKET_APP_PASSWORD", "TASK_NINJA_SECRET"}

# All recognized .env keys with defaults
ENV_DEFAULTS = {
    "TASK_NINJA_SECRET": "",  # auto-generated if empty
    "TASK_NINJA_HOST": "127.0.0.1",
    "TASK_NINJA_PORT": "8420",
    "TASK_NINJA_REMOTE_ACCESS": "false",
    "JIRA_BASE_URL": "",
    "JIRA_EMAIL": "",
    "JIRA_API_TOKEN": "",
    "BITBUCKET_WORKSPACE": "",
    "BITBUCKET_USERNAME": "",
    "BITBUCKET_APP_PASSWORD": "",
    "NOTIFICATIONS_ENABLED": "false",
    "VAPID_PUBLIC_KEY": "",
    "VAPID_PRIVATE_KEY": "",
    "VAPID_EMAIL": "",
    "AUTO_RETRY_ENABLED": "false",
    "AUTO_RETRY_DELAY_MINUTES": "15",
    "AUTO_RETRY_MAX": "3",
    "WORKING_HOURS_ENABLED": "false",
    "WORKING_HOURS_START": "09:00",
    "WORKING_HOURS_END": "18:00",
    "WORKING_HOURS_DAYS": "mon,tue,wed,thu,fri",
}


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict."""
    result = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Remove surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    """Write .env file with sections and comments."""
    sections = {
        "Server": ["TASK_NINJA_SECRET", "TASK_NINJA_HOST", "TASK_NINJA_PORT", "TASK_NINJA_REMOTE_ACCESS"],
        "Jira": ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"],
        "Bitbucket": ["BITBUCKET_WORKSPACE", "BITBUCKET_USERNAME", "BITBUCKET_APP_PASSWORD"],
        "Notifications": ["NOTIFICATIONS_ENABLED", "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_EMAIL"],
        "Scheduler": [
            "AUTO_RETRY_ENABLED", "AUTO_RETRY_DELAY_MINUTES", "AUTO_RETRY_MAX",
            "WORKING_HOURS_ENABLED", "WORKING_HOURS_START", "WORKING_HOURS_END", "WORKING_HOURS_DAYS",
        ],
    }

    lines = ["# Task Ninja Configuration", "# This file contains secrets — do not commit to git.", ""]
    for section, keys in sections.items():
        lines.append(f"# --- {section} ---")
        for key in keys:
            val = values.get(key, ENV_DEFAULTS.get(key, ""))
            lines.append(f"{key}={val}")
        lines.append("")

    # Write any extra keys not in sections
    known_keys = {k for keys in sections.values() for k in keys}
    for key, val in values.items():
        if key not in known_keys:
            lines.append(f"{key}={val}")

    path.write_text("\n".join(lines) + "\n")
    # Restrict permissions: owner read/write only
    os.chmod(path, 0o600)


def load_env() -> dict[str, str]:
    """Load .env file, create with defaults if missing. Returns merged dict."""
    current = _parse_env_file(ENV_PATH)

    # Auto-generate auth secret if missing
    if not current.get("TASK_NINJA_SECRET"):
        current["TASK_NINJA_SECRET"] = secrets.token_urlsafe(32)

    # Merge with defaults (existing values take precedence)
    merged = {**ENV_DEFAULTS, **current}

    # Write back (creates file if missing, adds new keys)
    _write_env_file(ENV_PATH, merged)

    # Also set as environment variables for other modules
    for key, val in merged.items():
        if val:
            os.environ[key] = val

    return merged


def get_env(key: str, default: str = "") -> str:
    """Get a single env value."""
    return os.environ.get(key, default)


def update_env(updates: dict[str, str]) -> dict[str, str]:
    """Update specific keys in .env and return the full config."""
    current = _parse_env_file(ENV_PATH)
    current.update(updates)

    # Re-merge with defaults
    merged = {**ENV_DEFAULTS, **current}
    _write_env_file(ENV_PATH, merged)

    # Update os.environ
    for key, val in updates.items():
        if val:
            os.environ[key] = val
        elif key in os.environ:
            del os.environ[key]

    return merged


def get_public_env() -> dict[str, str]:
    """Get env values safe to expose via API (secrets masked)."""
    current = _parse_env_file(ENV_PATH)
    merged = {**ENV_DEFAULTS, **current}
    result = {}
    for key, val in merged.items():
        if key in SECRET_KEYS and val:
            result[key] = val[:4] + "****" if len(val) > 4 else "****"
        else:
            result[key] = val
    return result

"""Read the operator secrets file. Values are never logged or printed."""

from pathlib import Path


def secrets_path() -> Path:
    return Path.home() / ".desk-secrets" / "env"


def parse_env_text(text: str) -> dict[str, str]:
    """KEY=value lines, plus export, set, and $env:NAME = value.

    Blank lines and # comments are skipped. Matching quotes around a value
    are removed. Later lines replace earlier ones. The text itself is never
    included in an error.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.startswith("export "):
            raw = raw[len("export ") :].strip()
        elif raw.lower().startswith("set "):
            raw = raw[4:].strip()
        if raw.startswith("$env:"):
            raw = raw[len("$env:") :]
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip().strip('"').strip("'")
        value = value.strip().strip('"').strip("'")
        if key:
            found[key] = value
    return found


def load_user_secrets(environ: dict | None = None, path: Path | None = None) -> None:
    """Fill empty keys from the secrets file. Existing values stay put."""
    import os

    env = environ if environ is not None else os.environ
    file = path if path is not None else secrets_path()
    if not file.is_file():
        return
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise OSError("could not read the secrets env file") from exc
    for key, value in parse_env_text(text).items():
        if not env.get(key):
            env[key] = value


def write_env_json(src: str, dest: str) -> None:
    """Used by run-desk2.ps1. Writes JSON and prints nothing."""
    import json

    path = Path(src)
    data = parse_env_text(path.read_text(encoding="utf-8")) if path.is_file() else {}
    Path(dest).write_text(json.dumps(data), encoding="utf-8")


def force_live_refused(environ: dict) -> bool:
    value = (environ.get("FORCE_LIVE") or "").strip().lower()
    return value not in ("", "0", "false", "no", "off")

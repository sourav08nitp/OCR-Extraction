"""Reads a .env file in the project root, so connection strings and keys live outside the code.

Real environment variables always win. OPENAI_API_KEY is already set for your Windows user, and a
stale line in .env should not silently shadow it - delete the line, or change the real variable.

.env is for secrets: keep it out of git and out of backups. .env.example lists the names with no
values, and is safe to share.
"""

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"


def load(path=ENV_FILE, override=False):
    """Read KEY=value lines. Blank lines and # comments are skipped, as is a leading "export ".
    Returns the names actually set, so a caller can say what it picked up."""
    path = Path(path)
    if not path.is_file():
        return []
    applied = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]      # quoted, e.g. a password with a # or a space in it
        if not key or (key in os.environ and not override):
            continue
        os.environ[key] = value
        applied.append(key)
    return applied


def describe(name):
    """A value safe to print: MongoDB URIs and API keys carry passwords."""
    v = os.environ.get(name)
    if not v:
        return "(not set)"
    if "://" in v and "@" in v:                       # mongodb+srv://user:pass@host/...
        scheme, _, rest = v.partition("://")
        # rsplit, not split: an @ inside the password would otherwise leave part of it showing
        return f"{scheme}://***@{rest.rsplit('@', 1)[1]}"
    return v if len(v) < 12 else v[:4] + "…" + v[-4:]

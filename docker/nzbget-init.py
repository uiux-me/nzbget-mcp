#!/usr/bin/env python3
"""Apply news-server settings from the environment into NZBGet's config.

Runs once before NZBGet starts, so `.env` is the single source of truth for
the stack: recreate the config volume and the provider is still configured.

NZBGet's own entrypoint only maps NZBGET_USER/NZBGET_PASS to command-line
overrides, so anything else has to be written into the config file itself.
This seeds that file the same way the entrypoint would, then sets only the
Server<slot>.* keys, leaving every other setting untouched.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

CONF = Path(os.environ.get("NZBGET_CONF", "/config/nzbget.conf"))
TEMPLATE = Path("/app/nzbget/share/nzbget/nzbget.conf")
WORKDIR = Path(os.environ.get("NZBGET_WORKDIR", "/downloads"))

# Transport settings for providers people commonly subscribe to; credentials
# always come from the environment. Mirrors PROVIDER_PRESETS in server.py.
PRESETS = {
    "newshosting": {"host": "news.newshosting.com", "port": "563", "encryption": "yes",
                    "connections": "30"},
    "usenetserver": {"host": "news.usenetserver.com", "port": "563", "encryption": "yes",
                     "connections": "20"},
    "eweka": {"host": "news.eweka.nl", "port": "563", "encryption": "yes",
              "connections": "20"},
}

TRUE = {"1", "true", "yes", "on"}


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def set_option(lines: list[str], key: str, value: str) -> list[str]:
    """Replace an option in place, or append it if it is not present."""
    prefix = f"{key}="
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"{key}={value}"
            return lines
    lines.append(f"{key}={value}")
    return lines


def fix_ownership() -> None:
    """Make the working tree owned by PUID:PGID, recursively.

    NZBGet's entrypoint chowns only the mount point. Directories created
    underneath it during an earlier run keep their old owner, so raising or
    lowering PUID/PGID afterwards leaves the daemon unable to write to its own
    queue and tmp directories — which shows up much later as a failed download
    rather than as a permissions error at startup.
    """
    if not WORKDIR.is_dir():
        return
    uid = int(env("PUID", "1000"))
    gid = int(env("PGID", "1000"))

    changed = 0
    for path in [WORKDIR, *WORKDIR.rglob("*")]:
        try:
            stat = path.lstat()
            if stat.st_uid != uid or stat.st_gid != gid:
                os.chown(path, uid, gid, follow_symlinks=False)
                changed += 1
        except (PermissionError, OSError):
            # Bind mounts from the host may refuse; NZBGet only needs the
            # volume-backed directories it actually writes state into.
            continue
    if changed:
        print(f"init: set ownership to {uid}:{gid} on {changed} path(s) under {WORKDIR}")


def main() -> int:
    fix_ownership()

    if not CONF.exists():
        if not TEMPLATE.exists():
            print(f"init: no config at {CONF} and no template to seed it from", file=sys.stderr)
            return 1
        CONF.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(TEMPLATE, CONF)
        print(f"init: seeded {CONF} from the shipped template")

    provider = env("NEWS_SERVER_PROVIDER").lower()
    if provider and provider not in PRESETS:
        print(
            f"init: unknown NEWS_SERVER_PROVIDER {provider!r}; "
            f"known: {', '.join(PRESETS)}. Set NEWS_SERVER_HOST explicitly instead.",
            file=sys.stderr,
        )
        return 1
    preset = PRESETS.get(provider, {})

    host = env("NEWS_SERVER_HOST", preset.get("host", ""))
    username = env("NEWS_SERVER_USERNAME")
    password = env("NEWS_SERVER_PASSWORD")

    if not host:
        print("init: no news server configured — leaving NZBGet's settings alone")
        return 0
    if not username or not password:
        print(
            "init: NEWS_SERVER_USERNAME and NEWS_SERVER_PASSWORD are required "
            f"when a provider or host is set (host={host})",
            file=sys.stderr,
        )
        return 1

    slot = env("NEWS_SERVER_SLOT", "1")
    encryption = env("NEWS_SERVER_ENCRYPTION", preset.get("encryption", "yes")).lower()
    encrypted = encryption in TRUE
    values = {
        "Active": "yes",
        "Name": env("NEWS_SERVER_NAME", provider or host),
        "Level": env("NEWS_SERVER_LEVEL", "0"),
        "Host": host,
        "Port": env("NEWS_SERVER_PORT", preset.get("port", "563")),
        "Username": username,
        "Password": password,
        "Encryption": "yes" if encrypted else "no",
        "Connections": env("NEWS_SERVER_CONNECTIONS", preset.get("connections", "8")),
        "CertVerification": "strict" if encrypted else "none",
    }

    lines = CONF.read_text().splitlines()
    for key, value in values.items():
        lines = set_option(lines, f"Server{slot}.{key}", value)
    CONF.write_text("\n".join(lines) + "\n")

    shown = ", ".join(
        f"{k}={'*' * 8 if k == 'Password' else v}" for k, v in values.items() if k != "Active"
    )
    print(f"init: wrote Server{slot} -> {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

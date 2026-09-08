"""The container init that turns .env into NZBGet news-server settings."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "docker" / "nzbget-init.py"

spec = importlib.util.spec_from_file_location("nzbget_init", SCRIPT)
init = importlib.util.module_from_spec(spec)
spec.loader.exec_module(init)

TEMPLATE = """# comment
MainDir=/downloads
Server1.Host=my.newsserver.com
Server1.Port=563
Server1.Username=user
Server1.Password=pass
ControlPassword=secret
"""


@pytest.fixture
def conf(tmp_path, monkeypatch):
    path = tmp_path / "nzbget.conf"
    path.write_text(TEMPLATE)
    monkeypatch.setattr(init, "CONF", path)
    for key in list(__import__("os").environ):
        if key.startswith("NEWS_SERVER_"):
            monkeypatch.delenv(key, raising=False)
    return path


def options(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and line[0] != "#"
    )


def test_no_configuration_leaves_the_file_alone(conf):
    assert init.main() == 0
    assert conf.read_text() == TEMPLATE


def test_a_preset_fills_in_the_transport_settings(conf, monkeypatch):
    monkeypatch.setenv("NEWS_SERVER_PROVIDER", "newshosting")
    monkeypatch.setenv("NEWS_SERVER_USERNAME", "me")
    monkeypatch.setenv("NEWS_SERVER_PASSWORD", "pw")
    assert init.main() == 0

    opts = options(conf)
    assert opts["Server1.Host"] == "news.newshosting.com"
    assert opts["Server1.Port"] == "563"
    assert opts["Server1.Username"] == "me"
    assert opts["Server1.Password"] == "pw"
    assert opts["Server1.Encryption"] == "yes"
    assert opts["Server1.CertVerification"] == "strict"
    assert opts["Server1.Connections"] == "30"
    # Unrelated settings survive.
    assert opts["MainDir"] == "/downloads"
    assert opts["ControlPassword"] == "secret"


def test_explicit_settings_override_the_preset(conf, monkeypatch):
    monkeypatch.setenv("NEWS_SERVER_PROVIDER", "newshosting")
    monkeypatch.setenv("NEWS_SERVER_USERNAME", "me")
    monkeypatch.setenv("NEWS_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("NEWS_SERVER_CONNECTIONS", "60")
    monkeypatch.setenv("NEWS_SERVER_PORT", "119")
    assert init.main() == 0

    opts = options(conf)
    assert opts["Server1.Connections"] == "60"
    assert opts["Server1.Port"] == "119"


def test_plain_connections_turn_off_certificate_checking(conf, monkeypatch):
    monkeypatch.setenv("NEWS_SERVER_HOST", "news.example.net")
    monkeypatch.setenv("NEWS_SERVER_ENCRYPTION", "no")
    monkeypatch.setenv("NEWS_SERVER_USERNAME", "me")
    monkeypatch.setenv("NEWS_SERVER_PASSWORD", "pw")
    assert init.main() == 0

    opts = options(conf)
    assert opts["Server1.Encryption"] == "no"
    assert opts["Server1.CertVerification"] == "none"


def test_a_second_slot_leaves_the_first_untouched(conf, monkeypatch):
    monkeypatch.setenv("NEWS_SERVER_PROVIDER", "eweka")
    monkeypatch.setenv("NEWS_SERVER_USERNAME", "me")
    monkeypatch.setenv("NEWS_SERVER_PASSWORD", "pw")
    monkeypatch.setenv("NEWS_SERVER_SLOT", "2")
    monkeypatch.setenv("NEWS_SERVER_LEVEL", "1")
    assert init.main() == 0

    opts = options(conf)
    assert opts["Server2.Host"] == "news.eweka.nl"
    assert opts["Server2.Level"] == "1"
    assert opts["Server1.Host"] == "my.newsserver.com"


def test_credentials_are_required_once_a_provider_is_named(conf, monkeypatch, capsys):
    monkeypatch.setenv("NEWS_SERVER_PROVIDER", "newshosting")
    assert init.main() == 1
    assert "NEWS_SERVER_USERNAME" in capsys.readouterr().err


def test_an_unknown_preset_fails_loudly(conf, monkeypatch, capsys):
    monkeypatch.setenv("NEWS_SERVER_PROVIDER", "skynet")
    monkeypatch.setenv("NEWS_SERVER_USERNAME", "me")
    monkeypatch.setenv("NEWS_SERVER_PASSWORD", "pw")
    assert init.main() == 1
    assert "unknown NEWS_SERVER_PROVIDER" in capsys.readouterr().err


def test_the_password_is_never_printed(conf, monkeypatch, capsys):
    monkeypatch.setenv("NEWS_SERVER_PROVIDER", "newshosting")
    monkeypatch.setenv("NEWS_SERVER_USERNAME", "me")
    monkeypatch.setenv("NEWS_SERVER_PASSWORD", "hunter2")
    init.main()
    assert "hunter2" not in capsys.readouterr().out


def test_passwords_with_regex_and_delimiter_characters_survive(conf, monkeypatch):
    """A sed-based implementation would mangle these; this one must not."""
    nasty = r"p@ss|word&with\slashes/and.$tars*"
    monkeypatch.setenv("NEWS_SERVER_PROVIDER", "newshosting")
    monkeypatch.setenv("NEWS_SERVER_USERNAME", "me")
    monkeypatch.setenv("NEWS_SERVER_PASSWORD", nasty)
    assert init.main() == 0
    assert options(conf)["Server1.Password"] == nasty


def test_a_missing_config_is_seeded_from_the_template(tmp_path, monkeypatch):
    conf = tmp_path / "nzbget.conf"
    template = tmp_path / "template.conf"
    template.write_text(TEMPLATE)
    monkeypatch.setattr(init, "CONF", conf)
    monkeypatch.setattr(init, "TEMPLATE", template)
    monkeypatch.setenv("NEWS_SERVER_PROVIDER", "newshosting")
    monkeypatch.setenv("NEWS_SERVER_USERNAME", "me")
    monkeypatch.setenv("NEWS_SERVER_PASSWORD", "pw")

    assert init.main() == 0
    assert options(conf)["Server1.Host"] == "news.newshosting.com"


# --- ownership ---------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    (root / "queue").mkdir(parents=True)
    (root / "tmp").mkdir()
    (root / "queue" / "state.txt").write_text("x")
    monkeypatch.setattr(init, "WORKDIR", root)
    for key in ("PUID", "PGID"):
        monkeypatch.delenv(key, raising=False)
    return root


def test_ownership_is_repaired_recursively(workdir, monkeypatch):
    """Raising PUID after the volume exists must not strand the subdirectories.

    NZBGet's entrypoint chowns only the mount point, so without this the daemon
    silently loses write access to queue/ and tmp/ and downloads fail later.
    """
    # Deliberately not the uid running the tests, or there would be nothing
    # to change and the assertion would pass vacuously.
    monkeypatch.setenv("PUID", "1234")
    monkeypatch.setenv("PGID", "5678")
    chowned: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        init.os, "chown",
        lambda p, u, g, follow_symlinks=True: chowned.append((Path(p).name, u, g)),
    )
    init.fix_ownership()

    names = {name for name, _, _ in chowned}
    assert {"downloads", "queue", "tmp", "state.txt"} <= names
    assert all((u, g) == (1234, 5678) for _, u, g in chowned)


def test_ownership_skips_paths_that_refuse(workdir, monkeypatch):
    """A host bind mount may reject chown; that must not abort the init."""
    monkeypatch.setenv("PUID", "1234")

    def refuse(path, uid, gid, follow_symlinks=True):
        if Path(path).name == "tmp":
            raise PermissionError("read-only bind mount")

    monkeypatch.setattr(init.os, "chown", refuse)
    init.fix_ownership()  # must not raise


def test_ownership_is_a_noop_without_a_workdir(tmp_path, monkeypatch):
    monkeypatch.setattr(init, "WORKDIR", tmp_path / "absent")
    init.fix_ownership()

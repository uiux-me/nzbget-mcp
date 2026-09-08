import pytest

from nzbget_mcp.config import load_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith(("NZBGET_", "MCP_")):
            monkeypatch.delenv(name, raising=False)


def test_defaults_match_nzbget_out_of_the_box(monkeypatch):
    settings = load_settings()
    assert settings.base_url == "http://localhost:6789"
    assert settings.rpc_url == "http://localhost:6789/jsonrpc"
    assert (settings.username, settings.password) == ("nzbget", "tegbzn6789")
    assert settings.transport == "http"


def test_host_and_port_build_the_url(monkeypatch):
    monkeypatch.setenv("NZBGET_HOST", "nzbget")
    monkeypatch.setenv("NZBGET_PORT", "6790")
    monkeypatch.setenv("NZBGET_SCHEME", "https")
    assert load_settings().base_url == "https://nzbget:6790"


def test_url_credentials_are_extracted_and_stripped(monkeypatch):
    monkeypatch.setenv("NZBGET_URL", "http://bob:s3cret@box.lan:6789")
    settings = load_settings()
    assert settings.base_url == "http://box.lan:6789"
    assert (settings.username, settings.password) == ("bob", "s3cret")


def test_explicit_credentials_beat_url_credentials(monkeypatch):
    monkeypatch.setenv("NZBGET_URL", "http://bob:s3cret@box.lan:6789")
    monkeypatch.setenv("NZBGET_PASSWORD", "override")
    assert load_settings().password == "override"


def test_subpath_is_preserved_and_jsonrpc_suffix_removed(monkeypatch):
    monkeypatch.setenv("NZBGET_URL", "https://home.example/nzbget/jsonrpc")
    settings = load_settings()
    assert settings.base_url == "https://home.example/nzbget"
    assert settings.rpc_url == "https://home.example/nzbget/jsonrpc"


def test_bare_host_gets_a_scheme(monkeypatch):
    monkeypatch.setenv("NZBGET_URL", "nzbget:6789")
    assert load_settings().base_url == "http://nzbget:6789"


def test_bad_boolean_is_rejected(monkeypatch):
    monkeypatch.setenv("NZBGET_READ_ONLY", "maybe")
    with pytest.raises(ValueError, match="NZBGET_READ_ONLY"):
        load_settings()


def test_bad_transport_is_rejected(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "carrier-pigeon")
    with pytest.raises(ValueError, match="MCP_TRANSPORT"):
        load_settings()


# --- indexers ----------------------------------------------------------------


def test_no_indexers_means_search_is_off(monkeypatch):
    assert load_settings().indexers == ()
    assert load_settings().search_enabled is False


def test_indexers_are_read_in_order(monkeypatch):
    monkeypatch.setenv("INDEXER1_URL", "https://api.nzbgeek.info")
    monkeypatch.setenv("INDEXER1_APIKEY", "k1")
    monkeypatch.setenv("INDEXER2_URL", "https://nzbfinder.ws")
    monkeypatch.setenv("INDEXER2_APIKEY", "k2")
    monkeypatch.setenv("INDEXER2_NAME", "finder")
    monkeypatch.setenv("INDEXER2_CATEGORIES", "5000, 2000")

    indexers = load_settings().indexers
    assert [i.name for i in indexers] == ["nzbgeek.info", "finder"]
    assert indexers[1].categories == (5000, 2000)
    assert load_settings().search_enabled is True


def test_a_gap_stops_the_scan(monkeypatch):
    monkeypatch.setenv("INDEXER1_URL", "https://one.test")
    monkeypatch.setenv("INDEXER1_APIKEY", "k1")
    monkeypatch.setenv("INDEXER3_URL", "https://three.test")
    monkeypatch.setenv("INDEXER3_APIKEY", "k3")
    assert [i.url for i in load_settings().indexers] == ["https://one.test"]


def test_a_url_without_a_key_is_an_error(monkeypatch):
    monkeypatch.setenv("INDEXER1_URL", "https://one.test")
    with pytest.raises(ValueError, match="INDEXER1_APIKEY"):
        load_settings()


def test_non_numeric_categories_are_rejected(monkeypatch):
    monkeypatch.setenv("INDEXER1_URL", "https://one.test")
    monkeypatch.setenv("INDEXER1_APIKEY", "k")
    monkeypatch.setenv("INDEXER1_CATEGORIES", "tv")
    with pytest.raises(ValueError, match="numeric Newznab ids"):
        load_settings()


def test_duplicate_indexer_names_are_rejected(monkeypatch):
    for n, url in ((1, "https://a.test"), (2, "https://b.test")):
        monkeypatch.setenv(f"INDEXER{n}_URL", url)
        monkeypatch.setenv(f"INDEXER{n}_APIKEY", "k")
        monkeypatch.setenv(f"INDEXER{n}_NAME", "same")
    with pytest.raises(ValueError, match="Duplicate indexer"):
        load_settings()


def test_search_can_be_disabled_with_indexers_still_configured(monkeypatch):
    monkeypatch.setenv("INDEXER1_URL", "https://one.test")
    monkeypatch.setenv("INDEXER1_APIKEY", "k")
    monkeypatch.setenv("SEARCH_ENABLED", "false")
    assert load_settings().search_enabled is False

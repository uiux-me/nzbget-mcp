"""Newznab parsing, indexer fan-out, ranking, and the result cache."""

from __future__ import annotations

import pytest

from nzbget_mcp.search import Indexer, NewznabError, ReleaseSearch, UnknownIndexerError
from nzbget_mcp.search.common import (
    SearchResult,
    human_size,
    parse_date,
    parse_size,
    resolve_categories,
)
from nzbget_mcp.search.newznab import parse_response

# Bound in a fixture-free way so the pure-parsing tests can use them too.
from conftest import FakeIndexers  # noqa: E402

FEED = FakeIndexers.feed
ITEM = FakeIndexers.item
from nzbget_mcp.search.registry import ResultCache


def _result(title="A.Release", indexer="geek", grabs=0, **kw) -> SearchResult:
    kw.setdefault("nzb_url", f"https://x/{title}.nzb")
    return SearchResult(title=title, indexer=indexer, grabs=grabs, **kw)


# --- parsing -----------------------------------------------------------------


def test_parses_attrs_and_prefers_the_enclosure_url():
    results = parse_response(FEED(ITEM("Show.S01E01", grabs=42)), "geek")
    assert len(results) == 1
    item = results[0]
    assert item.title == "Show.S01E01"
    assert item.size_bytes == 1_610_612_736
    assert item.grabs == 42
    assert item.files == 31
    assert item.group == "alt.binaries.teevee"
    # <link> is a details page; the enclosure is the actual NZB.
    assert item.nzb_url.endswith(".nzb")


def test_falls_back_to_link_when_there_is_no_enclosure():
    feed = FEED(
        "<item><title>Bare</title><guid>g1</guid>"
        "<link>https://idx.test/getnzb/g1.nzb</link></item>"
    )
    assert parse_response(feed, "geek")[0].nzb_url == "https://idx.test/getnzb/g1.nzb"


def test_enclosure_wins_over_a_useless_link():
    """usenet-crawler sets <link> to its own homepage, not the NZB.

    Real response shape: every item carries the same site-root <link>, so a
    parser that trusted <link> would queue the homepage instead of the release.
    """
    feed = FEED(
        "<item><title>Some.Release</title><guid>g1</guid>"
        "<link>https://www.usenet-crawler.com/</link>"
        '<enclosure url="https://www.usenet-crawler.com/getnzb/abc.nzb&amp;i=1&amp;r=KEY"'
        ' length="5400686" type="application/x-nzb"/>'
        '<newznab:attr name="grabs" value="10"/></item>'
    )
    result = parse_response(feed, "usenet-crawler")[0]
    assert result.nzb_url.startswith("https://www.usenet-crawler.com/getnzb/")
    assert "r=KEY" in result.nzb_url
    assert result.size_bytes == 5400686


def test_api_keys_in_urls_stay_out_of_the_default_payload():
    result = _result(nzb_url="https://idx/getnzb/x.nzb&r=SECRETKEY")
    assert "SECRETKEY" not in str(result.to_dict())
    assert "SECRETKEY" in str(result.to_dict(include_url=True))


def test_items_without_a_title_or_url_are_skipped():
    feed = FEED("<item><guid>g1</guid></item>", ITEM("Good"))
    results = parse_response(feed, "geek")
    assert [r.title for r in results] == ["Good"]


def test_credential_errors_come_back_as_http_200_bodies():
    with pytest.raises(NewznabError, match="the API key is wrong"):
        parse_response('<error code="100" description="Incorrect user credentials"/>', "geek")


def test_a_valid_key_on_an_unentitled_account_is_not_blamed_on_the_key():
    """NZBGeek answers a good key on a trial account with 101.

    Telling someone to check a key that is already correct sends them the
    wrong way, so 101 must read as an account problem, not a key problem.
    """
    with pytest.raises(NewznabError, match="not entitled to API access") as exc:
        parse_response('<error code="101" description="Trial Account Only"/>', "geek")
    assert "API key is wrong" not in str(exc.value)


def test_unknown_error_codes_keep_their_description_without_a_hint():
    with pytest.raises(NewznabError, match="Request limit reached") as exc:
        parse_response('<error code="500" description="Request limit reached"/>', "geek")
    assert "(" not in str(exc.value).split("Request limit reached")[-1]


def test_garbage_responses_are_reported_not_raised_as_parse_errors():
    with pytest.raises(NewznabError, match="unparseable"):
        parse_response("<html>maintenance</html>!!", "geek")


def test_result_id_is_stable_and_indexer_specific():
    a = _result(guid="g1", indexer="geek")
    b = _result(guid="g1", indexer="finder")
    assert a.result_id == _result(guid="g1", indexer="geek").result_id
    assert a.result_id != b.result_id


def test_age_days_is_derived_from_the_posted_date():
    assert _result(posted="2020-01-01").age_days > 2000
    assert _result().age_days is None


# --- fan-out and ranking -----------------------------------------------------

TWO = [Indexer("geek", "https://geek.test", "k1"), Indexer("finder", "https://finder.test", "k2")]


async def test_searches_every_indexer_and_passes_the_api_key(indexers):
    indexers.bodies["geek.test"] = FEED(ITEM("Alpha"))
    indexers.bodies["finder.test"] = FEED(ITEM("Beta"))
    results, report = await indexers.search(TWO).search("query")

    assert {r.title for r in results} == {"Alpha", "Beta"}
    assert report == {"geek": {"ok": True, "results": 1}, "finder": {"ok": True, "results": 1}}
    assert indexers.params_for("geek.test")["apikey"] == "k1"
    assert indexers.params_for("finder.test")["apikey"] == "k2"


async def test_duplicate_releases_collapse_keeping_the_most_grabbed(indexers):
    indexers.bodies["geek.test"] = FEED(ITEM("Show.S01E01", guid="a", grabs=3))
    indexers.bodies["finder.test"] = FEED(ITEM("show.s01e01", guid="b", grabs=90))
    results, _ = await indexers.search(TWO).search("show")

    assert len(results) == 1
    assert results[0].indexer == "finder" and results[0].grabs == 90


async def test_results_rank_by_grabs(indexers):
    indexers.bodies["geek.test"] = FEED(
        ITEM("Low", grabs=1), ITEM("High", grabs=500), ITEM("Mid", grabs=50)
    )
    results, _ = await indexers.search().search("x")
    assert [r.title for r in results] == ["High", "Mid", "Low"]


async def test_a_failing_indexer_is_reported_not_fatal(indexers):
    indexers.bodies["geek.test"] = FEED(ITEM("Alpha"))
    indexers.status["finder.test"] = 503
    results, report = await indexers.search(TWO).search("x")

    assert [r.title for r in results] == ["Alpha"]
    assert report["geek"]["ok"] is True
    assert report["finder"]["ok"] is False and "503" in report["finder"]["error"]


async def test_categories_are_sent_as_newznab_ids(indexers):
    await indexers.search().search("x", categories=resolve_categories(["tv", "movies"]))
    assert indexers.params_for("geek.test")["cat"] == "5000,2000"


async def test_per_indexer_categories_apply_when_none_requested(indexers):
    scoped = [Indexer("geek", "https://geek.test", "k1", categories=(5000,))]
    await indexers.search(scoped).search("x")
    assert indexers.params_for("geek.test")["cat"] == "5000"


async def test_unknown_indexers_are_rejected(indexers):
    with pytest.raises(UnknownIndexerError, match="Unknown indexer"):
        await indexers.search(TWO).search("x", indexers=["nope"])


async def test_search_can_be_restricted_to_one_indexer(indexers):
    indexers.bodies["geek.test"] = FEED(ITEM("Alpha"))
    indexers.bodies["finder.test"] = FEED(ITEM("Beta"))
    results, report = await indexers.search(TWO).search("x", indexers=["geek"])
    assert [r.title for r in results] == ["Alpha"] and "finder" not in report


def test_duplicate_indexer_names_are_rejected():
    with pytest.raises(ValueError, match="Duplicate indexer"):
        ReleaseSearch([Indexer("a", "https://x", "k"), Indexer("a", "https://y", "k")])


def test_at_least_one_indexer_is_required():
    with pytest.raises(ValueError, match="at least one indexer"):
        ReleaseSearch([])


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://api.nzbgeek.info", "https://api.nzbgeek.info/api"),
        ("https://hydra.lan/api/", "https://hydra.lan/api"),
        ("https://idx.test/", "https://idx.test/api"),
    ],
)
def test_api_url_normalization(url, expected):
    assert Indexer("n", url, "k").api_url == expected


# --- cache -------------------------------------------------------------------


def test_cache_returns_results_then_expires_them():
    cache = ResultCache(ttl=900)
    hit = _result(guid="g1")
    cache.put([hit])
    assert cache.get(hit.result_id).title == hit.title

    expired = ResultCache(ttl=-1)
    expired.put([hit])
    assert expired.get(hit.result_id) is None


def test_cache_misses_are_none():
    assert ResultCache().get("nosuchid") is None


# --- helpers -----------------------------------------------------------------


def test_unknown_categories_are_rejected_with_the_known_list():
    with pytest.raises(ValueError, match="Known:"):
        resolve_categories(["telly"])


def test_numeric_categories_pass_through():
    assert resolve_categories(["5070", "tv"]) == [5070, 5000]


@pytest.mark.parametrize(
    "value,expected",
    [(1024, 1024), ("1024", 1024), ("5.7 GiB", 6120328396), ("", None), (None, None), (0, None)],
)
def test_size_parsing(value, expected):
    assert parse_size(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (1700000000, "2023-11-14"),
        ("Wed, 19 Aug 2026 00:36:55 -0000", "2026-08-19"),
        ("2026-08-19T00:36:55Z", "2026-08-19"),
        ("nonsense", None),
    ],
)
def test_date_parsing(value, expected):
    assert parse_date(value) == expected


def test_human_size():
    assert human_size(1_610_612_736) == "1.50 GiB"
    assert human_size(0) is None

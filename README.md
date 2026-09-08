# nzbget-mcp

A [FastMCP](https://gofastmcp.com) server that wraps the
[NZBGet](https://nzbget.com) JSON-RPC API, packaged for Docker. It lets an MCP
client (Claude Code, Claude Desktop, or anything else that speaks MCP) inspect
and control a running NZBGet instance — the one you installed by following
[Installation on Linux](https://nzbget.github.io/installation-on-linux), or any
other deployment reachable over HTTP.

This server does **not** install or bundle NZBGet. It talks to NZBGet's RPC
endpoint at `<base-url>/jsonrpc`, which listens on port `6789` by default.

## Tools

| Tool | What it does |
| --- | --- |
| `get_status` | Speed, remaining queue size, pause state, free disk space, uptime |
| `list_queue` | Active and queued downloads, with progress percentages |
| `list_files` | Individual files inside a queued download |
| `get_history` | Finished downloads with success/failure and par/unpack status |
| `get_log` | Recent server log entries, or the log of a single download |
| `get_version` | NZBGet version string |
| `get_server_volumes` | Downloaded volume per news server |
| `get_config` | Configuration options (password-like values are redacted) |
| `get_system_info` | Host OS, CPU and disk info (NZBGet 24.2+) |
| `search_releases` | Search Newznab indexers in parallel, ranked and deduplicated |
| `test_news_server` | Verify provider credentials (DNS, TLS and login) |
| `configure_news_server` | Save a usenet provider into NZBGet's news server settings |
| `add_nzb` | Queue an NZB by search result, URL, local file, or base64 content |
| `manage_downloads` | Pause, resume, delete, reorder, recategorise, sort queued items |
| `manage_history` | Requeue, redownload, re-post-process, mark, or delete history items |
| `set_paused` | Pause/resume downloading, post-processing, or folder scanning |
| `schedule_resume` | Resume everything automatically after N seconds |
| `set_speed_limit` | Set the speed limit in KB/s (0 removes it) |
| `scan_incoming` | Rescan the incoming `NzbDir` for new `.nzb` files |
| `write_log` | Append a message to NZBGet's log |
| `reload_server` | Reload NZBGet and re-read its configuration |
| `shutdown_server` | Shut NZBGet down (only when `NZBGET_ALLOW_SHUTDOWN=true`) |

Search tools appear only when at least one indexer is configured.

Resources mirror the read-only views for clients that prefer them:
`nzbget://status`, `nzbget://queue`, `nzbget://history`, and
`search://indexers`.

## Configuring your provider

Put the credentials in `.env` and they are applied to NZBGet at startup:

```bash
NEWS_SERVER_PROVIDER=newshosting     # or usenetserver, eweka
NEWS_SERVER_USERNAME=your-username
NEWS_SERVER_PASSWORD=your-password
NEWS_SERVER_CONNECTIONS=30           # what your plan allows
```

A preset fills in host, port, encryption and a sane connection count. For any
other provider set `NEWS_SERVER_HOST` / `NEWS_SERVER_PORT` instead. Backup
providers go in higher slots with `NEWS_SERVER_SLOT=2` and `NEWS_SERVER_LEVEL=1`.

`nzbget-init` also repairs ownership under `/downloads` to match `PUID`/`PGID`
on every start. NZBGet's own entrypoint chowns only the mount point, so
changing those values after the volume exists would otherwise strand the
subdirectories it created earlier — which surfaces later as a failed download
rather than as a permissions error at startup.

A one-shot `nzbget-init` container writes these into NZBGet's config before it
starts, so `.env` stays the source of truth: destroy the config volume and the
provider is still configured on the next `docker compose up`. Only the
`Server<slot>.*` keys are touched — everything else you have changed in the web
UI survives. The flip side is that these keys are reapplied on every start, so
edit them in `.env` rather than the web UI.

The `configure_news_server` tool does the same job at runtime, which is what
you want when NZBGet is not managed by this compose file. Either way,
`test_news_server` verifies DNS, TLS and login.

## Provider vs indexer

These are two different products and you need both:

- A **provider** (Newshosting, UsenetServer, Eweka…) sells access to the
  articles. This is what NZBGet connects to over NNTP. Configure it with
  `configure_news_server` — there are presets for the common ones, so
  Newshosting needs only a username and password.
- An **indexer** (NZBGeek, NZBFinder, DrunkenSlug…) sells a searchable
  catalogue of NZBs, exposed over the Newznab API. This is what
  `search_releases` queries.

A provider's own search — Newshosting's, for instance — lives inside its
proprietary newsreader and has no API, so it cannot be used as a search source
here or anywhere else.

## Search

Configure one or more Newznab indexers (see `.env.example`), then:

```
search_releases(query="some show s01e01", categories=["tv"])
→ add_nzb(result_id="…")
```

Indexers are queried in parallel and results merged. Releases carried by more
than one indexer collapse to a single entry, keeping whichever copy has been
grabbed most. An indexer that is down or rate-limited is reported in
`indexers_failed` rather than failing the search, because a blocked indexer
changes how much an empty result set should be trusted.

Ranking is by `grabs`, then recency. Usenet has no seeder count, so grabs — how
many people have already taken a release — is the best available proxy for a
post being complete, and `age_days` flags releases old enough to have decayed
past a provider's retention.

Result URLs embed your API key, so they are omitted unless you pass
`include_urls=true`; `add_nzb` only needs the `result_id`, which is resolved
from a short-lived cache.

Aggregators work as-is: NZBHydra2 and Prowlarr both expose a Newznab endpoint,
so point `INDEXER1_URL` at that and every indexer behind it comes along.

Give the site root as the URL, not the `/api` path — that gets appended:

```bash
INDEXER1_NAME=usenet-crawler
INDEXER1_URL=https://www.usenet-crawler.com
INDEXER1_APIKEY=your-api-key
```

### Response shaping

NZBGet's raw structs are verbose and split 64-bit numbers across
`FieldLo`/`FieldHi` pairs. This server merges those into single byte values,
drops fields the API documents as deprecated, converts Unix timestamps to
ISO-8601 UTC, and returns a curated subset of fields by default. Pass
`verbose=true` to any read tool to get the full normalized struct instead.

## Configuration

All configuration is via environment variables.

| Variable | Default | Purpose |
| --- | --- | --- |
| `NZBGET_URL` | — | Full base URL, e.g. `http://nzbget:6789`. Credentials may be embedded. Takes precedence over the host/port pair below. |
| `NZBGET_SCHEME` | `http` | Used when `NZBGET_URL` is unset |
| `NZBGET_HOST` | `localhost` | Used when `NZBGET_URL` is unset |
| `NZBGET_PORT` | `6789` | Used when `NZBGET_URL` is unset |
| `NZBGET_USERNAME` | `nzbget` | NZBGet `ControlUsername` |
| `NZBGET_PASSWORD` | `tegbzn6789` | NZBGet `ControlPassword` |
| `NZBGET_TIMEOUT` | `30` | HTTP timeout in seconds |
| `NZBGET_VERIFY_SSL` | `true` | Set `false` for a self-signed HTTPS certificate |
| `NZBGET_READ_ONLY` | `false` | When true, only the read tools are registered |
| `NZBGET_ALLOW_SHUTDOWN` | `false` | Registers `shutdown_server` when true |
| `MCP_TRANSPORT` | `http` | `http` or `stdio` |
| `MCP_HOST` | `0.0.0.0` | HTTP bind address |
| `MCP_PORT` | `8000` | HTTP port |
| `MCP_PATH` | `/mcp` | HTTP endpoint path |
| `LOG_LEVEL` | `info` | Uvicorn log level |

Change NZBGet's default password before exposing anything to a network: the
RPC endpoint uses HTTP basic auth, which is Base64-encoded rather than
encrypted.

## Run with Docker

The stack is self-contained — NZBGet and the MCP server together, no host
dependencies beyond Docker:

```bash
docker compose up -d
```

That gives you NZBGet's web UI on <http://127.0.0.1:6789> and the MCP endpoint
on <http://127.0.0.1:8080/mcp>. Default NZBGet login is `nzbget` /
`tegbzn6789`; change it via `.env` (copy `.env.example`) before doing anything
real.

### Storage layout

NZBGet's working directories — `intermediate`, `queue`, `tmp`, `nzb` — live in
the `nzbget-work` named volume, so their heavy write traffic stays inside
Docker's VM and runs at native speed. This matters on macOS, where bind mounts
cross a virtualisation boundary and a downloader is exactly the workload that
exposes it.

Only `./completed` is bind-mounted back to the host, so finished downloads are
reachable from Finder. That path sees one sequential write per completed file,
which the bind mount handles fine.

To put completed files somewhere else, point the bind mount at it:

```yaml
volumes:
  - /Volumes/Media/usenet:/downloads/completed
```

### Ports

Both ports publish to `127.0.0.1` only. The MCP endpoint has **no
authentication** — anything that reaches it controls the downloader — so don't
change those to `0.0.0.0` without putting something in front. Override the host
port with `MCP_PORT` if 8080 is taken.

### Tuning

The image ships conservative defaults (`ArticleCache=0`, `WriteBuffer=0`,
`ParBuffer=16`). On a machine with RAM to spare, these are worth raising —
NZBGet's guidance is a 200 MB article cache when `DirectWrite` is on (it is by
default here), a 1024 KB write buffer, and a few hundred MB of par buffer for
repair speed:

```bash
docker compose exec nzbget sh -c \
  "sed -i 's/^ArticleCache=.*/ArticleCache=200/; \
           s/^WriteBuffer=.*/WriteBuffer=1024/; \
           s/^ParBuffer=.*/ParBuffer=500/' /config/nzbget.conf"
docker compose restart nzbget
```

Settings persist in the `nzbget-config` volume.

### Pointing at an existing NZBGet instead

To skip the bundled NZBGet and drive one you already run, start just the MCP
service and override the URL:

```bash
NZBGET_URL=http://host.docker.internal:6789 docker compose up -d mcp
```

## Connect a client

HTTP transport (the Docker default):

```bash
claude mcp add --transport http nzbget http://127.0.0.1:8080/mcp
```

stdio transport, running the container per-session:

```json
{
  "mcpServers": {
    "nzbget": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "MCP_TRANSPORT=stdio",
        "-e", "NZBGET_URL=http://host.docker.internal:6789",
        "-e", "NZBGET_PASSWORD=your-password",
        "nzbget-mcp"
      ]
    }
  }
}
```

## Deploying to a low-memory host

For constrained hardware (a Raspberry Pi and similar), Docker is the wrong
shape — see [`deploy/README.md`](deploy/README.md) for a native install with
tuning, a systemd guard that keeps downloads off the boot medium, and an
on-demand SSH transport that uses no RAM while idle.

## Local development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
NZBGET_URL=http://localhost:6789 MCP_TRANSPORT=stdio .venv/bin/nzbget-mcp
```

## Compatibility

Targets the NZBGet RPC API as documented for v13.0 and later. `editqueue` changed
signature in v18.0; the client detects the running version and adapts. Two tools
need newer builds: `get_system_info` requires v24.2+, and the `mark_success`
history action requires v15.0+.

## Sources

- [NZBGet installation on Linux](https://nzbget.github.io/installation-on-linux)
- [NZBGet API reference](https://nzbget.com/documentation/api/)

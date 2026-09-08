# Deploying to the Pi (`piracy`)

Target: Raspberry Pi 3 B+, Debian 13 arm64, 905 MB RAM, 4× Cortex-A53 @ 1.4 GHz.

Two things drive every decision here: downloads must not touch the SD card, and
nothing may sit idle eating RAM.

## 1. Prepare the USB drive

NZBGet writes roughly 2.5× a release's size across its download → verify →
unpack → move cycle. That belongs on external storage, not on the card holding
your root filesystem.

Format as **ext4**. exFAT and NTFS lack the sparse-file support NZBGet's
`DirectWrite` relies on, which costs you roughly double the disk operations.

```bash
lsblk -o NAME,SIZE,TRAN,MOUNTPOINT          # identify the drive — check twice
sudo mkfs.ext4 -L downloads /dev/sdXY       # DESTROYS everything on that device
sudo mkdir -p /mnt/downloads
echo "LABEL=downloads /mnt/downloads ext4 defaults,noatime,nofail 0 2" | sudo tee -a /etc/fstab
sudo mount -a
```

`noatime` avoids a metadata write on every read; `nofail` keeps the Pi booting
if the drive is absent.

## 2. Install

```bash
deploy/push.sh piracy
```

Syncs the source, installs NZBGet 26.2 from the official arm64 `.deb`, applies
the tuning below, installs the MCP server into `/opt/nzbget-mcp`, and writes
credentials to `/etc/nzbget-mcp.env`.

Safe to run before the drive exists — a systemd drop-in sets
`RequiresMountsFor=/mnt/downloads`, so NZBGet refuses to start rather than
silently recreating its download directories on the SD card. Re-run the script
once the drive is mounted.

Then change `ControlPassword` in the web UI (`http://piracy:6789`, default
`nzbget` / `tegbzn6789`) and re-run `deploy/push.sh` to sync the new password.

## 3. Connect the MCP server

Recommended — spawn it over SSH on demand, so nothing runs while idle:

```bash
claude mcp add nzbget -- ssh piracy /opt/nzbget-mcp/venv/bin/nzbget-mcp
```

Transport is already `stdio` via `/etc/nzbget-mcp.env`. SSH keys do the
authenticating, no port is exposed, and the process exists only for the life of
the session — worth having on a box with 625 MB free.

For an always-on HTTP endpoint instead, install `deploy/nzbget-mcp.service`. It
binds loopback only and caps memory at 200 MB; reach it with an SSH tunnel
(`ssh -L 8000:127.0.0.1:8000 piracy`). **The MCP endpoint has no
authentication** — anything that reaches it controls the downloader, so do not
bind it to the LAN.

The Docker setup in the repo root stays valid for better hardware. It is the
wrong shape here: dockerd plus containerd would claim ~100 MB of the 625 MB
available, and NZBGet is a single self-contained binary with no dependency mess
worth isolating.

## Tuning applied, and why

| Setting | Default | Here | Reason |
| --- | --- | --- | --- |
| `MainDir` | `/var/lib/nzbget/downloads` | `/mnt/downloads` | `DestDir`, `InterDir`, `NzbDir` and `QueueDir` all derive from it, so one setting moves everything off the SD card |
| `TempDir` | `/var/lib/nzbget/tmp` | `${MainDir}/tmp` | The one working directory that does not derive from `MainDir` |
| `ArticleCache` | 100 MB | 50 MB | Peaks alongside `ParBuffer`; 200 MB combined is too much of 905 MB |
| `ParBuffer` | 100 MB | 50 MB | Same |
| `PostStrategy` | `balanced` | `sequential` | `balanced` runs a par repair beside another task; four 1.4 GHz cores cannot absorb that |
| `ParPauseQueue` | `no` | `yes` | Stop downloading during repair so the cores aren't split |
| `UnpackPauseQueue` | `no` | `yes` | Same, for unpack — NZBGet recommends it explicitly for slow CPUs |
| `DiskSpace` | 250 MB | 5000 MB | Unpacking needs room for a second copy of the release |
| `DirectWrite` | `yes` | `yes` | Kept — ext4 sparse files roughly halve disk operations |

## Expected performance

The USB 2.0 bus caps the drive near 40 MB/s, and the Pi 3 B+ puts its ethernet
on that same bus, capping the network around 300 Mbit/s (~37 MB/s). Those land
in the same place, so expect roughly 200–300 Mbit/s while downloading, and
noticeably longer for par repair and unpack than the download itself — those are
CPU-bound on cores far slower than the disk.

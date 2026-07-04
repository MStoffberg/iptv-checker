#!/usr/bin/env python3

import concurrent.futures
import gzip
import html
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SOURCES_DIR = ROOT / "sources"
PUBLIC_DIR = ROOT / "public"
ASSETS_DIR = PUBLIC_DIR / "assets"
DATA_DIR = PUBLIC_DIR / "data"

PLAYLISTS_FILE = SOURCES_DIR / "playlists.txt"
EPGS_FILE = SOURCES_DIR / "epgs.txt"

SOURCE_M3U = PUBLIC_DIR / "source.m3u"
WORKING_M3U = PUBLIC_DIR / "working.m3u"
BROKEN_TXT = PUBLIC_DIR / "broken.txt"

EPG_XML = PUBLIC_DIR / "epg.xml"
EPG_XML_GZ = PUBLIC_DIR / "epg.xml.gz"

REPORT_JSON = DATA_DIR / "report.json"
CHECK_LOG = DATA_DIR / "check.log"

INDEX_HTML = PUBLIC_DIR / "index.html"
STYLE_CSS = ASSETS_DIR / "style.css"
APP_JS = ASSETS_DIR / "app.js"

TIMEOUT_SECONDS = 10
MAX_WORKERS = 20


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def console(message):
    print(message, flush=True)


def log(message):
    line = f"[{now_iso()}] {message}"
    print(line, flush=True)

    with CHECK_LOG.open("a", encoding="utf-8") as file:
        file.write(line + "\n")


def section(title):
    console("")
    console(f"========== {title} ==========")
    log(title)


def read_url_list(path: Path):
    if not path.exists():
        return []

    urls = []

    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        urls.append(line)

    return urls


def download_text(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 stoffcore-free-tv-checker"},
    )

    with urllib.request.urlopen(req, timeout=45) as response:
        data = response.read()

    if url.endswith(".gz"):
        data = gzip.decompress(data)

    return data.decode("utf-8", errors="ignore")


def download_binary(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 stoffcore-free-tv-checker"},
    )

    with urllib.request.urlopen(req, timeout=60) as response:
        data = response.read()

    if url.endswith(".gz"):
        data = gzip.decompress(data)

    return data


def parse_m3u(text: str):
    entries = []
    current_info = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        if line.startswith("#EXTINF"):
            current_info = line
        elif line.startswith("http"):
            if current_info:
                entries.append((current_info, line))
                current_info = None

    return entries


def channel_name(extinf: str):
    if "," in extinf:
        return extinf.rsplit(",", 1)[1].strip()

    return "Unknown"


def get_attr(extinf: str, attr: str):
    token = f'{attr}="'

    if token not in extinf:
        return ""

    start = extinf.find(token) + len(token)
    end = extinf.find('"', start)

    if end == -1:
        return ""

    return extinf[start:end]


def test_stream(entry):
    extinf, url, source_playlist = entry
    name = channel_name(extinf)
    group_title = get_attr(extinf, "group-title") or "Ungrouped"

    start_time = time.time()

    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-rw_timeout",
        str(TIMEOUT_SECONDS * 1000000),
        "-timeout",
        str(TIMEOUT_SECONDS * 1000000),
        "-i",
        url,
        "-show_entries",
        "stream=codec_type",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT_SECONDS + 3,
            text=True,
        )

        duration_ms = round((time.time() - start_time) * 1000)
        output = result.stdout.lower()

        ok = result.returncode == 0 and ("video" in output or "audio" in output)

        error = ""
        if not ok:
            error = result.stderr.strip()[:500] or "No playable video/audio stream detected"

        return {
            "ok": ok,
            "name": name,
            "url": url,
            "extinf": extinf,
            "source_playlist": source_playlist,
            "duration_ms": duration_ms,
            "error": error,
            "tvg_id": get_attr(extinf, "tvg-id"),
            "tvg_name": get_attr(extinf, "tvg-name"),
            "group_title": group_title,
            "logo": get_attr(extinf, "tvg-logo"),
        }

    except Exception as error:
        duration_ms = round((time.time() - start_time) * 1000)

        return {
            "ok": False,
            "name": name,
            "url": url,
            "extinf": extinf,
            "source_playlist": source_playlist,
            "duration_ms": duration_ms,
            "error": str(error)[:500],
            "tvg_id": get_attr(extinf, "tvg-id"),
            "tvg_name": get_attr(extinf, "tvg-name"),
            "group_title": group_title,
            "logo": get_attr(extinf, "tvg-logo"),
        }


def build_source_playlist():
    section("📺 Downloading playlists")

    playlist_urls = read_url_list(PLAYLISTS_FILE)

    if not playlist_urls:
        raise RuntimeError(f"No playlist URLs found in {PLAYLISTS_FILE}")

    all_entries = []
    seen_urls = set()

    for playlist_url in playlist_urls:
        log(f"Downloading playlist: {playlist_url}")

        try:
            text = download_text(playlist_url)
            entries = parse_m3u(text)

            added = 0
            duplicates = 0

            for extinf, stream_url in entries:
                if stream_url in seen_urls:
                    duplicates += 1
                    continue

                seen_urls.add(stream_url)
                all_entries.append((extinf, stream_url, playlist_url))
                added += 1

            console(f"✅ Playlist loaded | found={len(entries)} added={added} duplicates={duplicates}")
            log(
                f"Playlist loaded: {playlist_url} | "
                f"found={len(entries)} added={added} duplicates={duplicates}"
            )

        except Exception as error:
            console(f"❌ Playlist failed: {playlist_url}")
            log(f"FAILED playlist: {playlist_url} | error={error}")

    SOURCE_M3U.write_text(
        "#EXTM3U\n"
        + "\n".join([f"{extinf}\n{url}" for extinf, url, _ in all_entries])
        + "\n",
        encoding="utf-8",
    )

    console(f"📦 Total unique channels found: {len(all_entries)}")

    return all_entries


def build_epg():
    section("📘 Building EPG")

    epg_urls = read_url_list(EPGS_FILE)

    epg_report = {
        "sources": [],
        "channel_count": 0,
        "programme_count": 0,
    }

    if not epg_urls:
        console("⚠️ No EPG URLs configured.")
        log("No EPG URLs configured.")
        return epg_report

    channels = []
    programmes = []

    for epg_url in epg_urls:
        source_info = {
            "url": epg_url,
            "ok": False,
            "channels": 0,
            "programmes": 0,
            "error": "",
        }

        log(f"Downloading EPG: {epg_url}")

        try:
            xml = download_binary(epg_url).decode("utf-8", errors="ignore")

            channel_count = 0
            programme_count = 0
            inside_programme = False

            for line in xml.splitlines():
                clean = line.strip()

                if clean.startswith("<channel "):
                    channels.append(clean)
                    channel_count += 1

                elif clean.startswith("<programme "):
                    programmes.append(clean)
                    programme_count += 1
                    inside_programme = True

                elif inside_programme:
                    programmes.append(clean)

                    if clean.startswith("</programme>"):
                        inside_programme = False

            source_info["ok"] = True
            source_info["channels"] = channel_count
            source_info["programmes"] = programme_count

            console(f"✅ EPG loaded | channels={channel_count} programmes={programme_count}")
            log(
                f"EPG loaded: {epg_url} | "
                f"channels={channel_count} programmes={programme_count}"
            )

        except Exception as error:
            source_info["error"] = str(error)
            console(f"❌ EPG failed: {epg_url}")
            log(f"FAILED EPG: {epg_url} | error={error}")

        epg_report["sources"].append(source_info)

    seen_channels = set()
    unique_channels = []

    for channel in channels:
        if channel in seen_channels:
            continue

        seen_channels.add(channel)
        unique_channels.append(channel)

    merged = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>", "<tv>"]
    merged.extend(unique_channels)
    merged.extend(programmes)
    merged.append("</tv>")
    merged.append("")

    EPG_XML.write_text("\n".join(merged), encoding="utf-8")

    with open(EPG_XML, "rb") as src:
        with gzip.open(EPG_XML_GZ, "wb") as dst:
            shutil.copyfileobj(src, dst)

    epg_report["channel_count"] = len(unique_channels)
    epg_report["programme_count"] = len(programmes)

    console(f"📘 EPG saved | unique_channels={len(unique_channels)} programme_lines={len(programmes)}")
    log(
        f"EPG saved: {EPG_XML_GZ} | "
        f"unique_channels={len(unique_channels)} programme_lines={len(programmes)}"
    )

    return epg_report


def write_outputs(results, epg_report, started_at, finished_at):
    section("💾 Writing output files")

    working = [item for item in results if item["ok"]]
    broken = [item for item in results if not item["ok"]]

    with open(WORKING_M3U, "w", encoding="utf-8") as file:
        file.write("#EXTM3U\n")

        for item in working:
            file.write(item["extinf"] + "\n")
            file.write(item["url"] + "\n")

    with open(BROKEN_TXT, "w", encoding="utf-8") as file:
        for item in broken:
            file.write(f"Name: {item['name']}\n")
            file.write(f"Group: {item['group_title']}\n")
            file.write(f"URL: {item['url']}\n")
            file.write(f"Source: {item['source_playlist']}\n")
            file.write(f"Error: {item['error']}\n")
            file.write("\n")

    group_stats = {}

    for item in results:
        group = item["group_title"] or "Ungrouped"

        if group not in group_stats:
            group_stats[group] = {
                "total": 0,
                "working": 0,
                "broken": 0,
            }

        group_stats[group]["total"] += 1

        if item["ok"]:
            group_stats[group]["working"] += 1
        else:
            group_stats[group]["broken"] += 1

    report = {
        "started_at": started_at,
        "finished_at": finished_at,
        "total_channels": len(results),
        "working_channels": len(working),
        "broken_channels": len(broken),
        "success_rate_percent": round((len(working) / len(results)) * 100, 2) if results else 0,
        "timeout_seconds": TIMEOUT_SECONDS,
        "max_workers": MAX_WORKERS,
        "group_stats": group_stats,
        "epg": epg_report,
        "working": working,
        "broken": broken,
    }

    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    write_html_files()

    console(f"✅ Saved {WORKING_M3U}")
    console(f"✅ Saved {BROKEN_TXT}")
    console(f"✅ Saved {REPORT_JSON}")
    console(f"✅ Saved {INDEX_HTML}")
    console(f"✅ Saved {STYLE_CSS}")
    console(f"✅ Saved {APP_JS}")

    log(f"Saved: {WORKING_M3U}")
    log(f"Saved: {BROKEN_TXT}")
    log(f"Saved: {REPORT_JSON}")
    log(f"Saved: {INDEX_HTML}")
    log(f"Saved: {STYLE_CSS}")
    log(f"Saved: {APP_JS}")


def write_html_files():
    INDEX_HTML.write_text(
        """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>StoffCore Free TV Status</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="assets/style.css">
</head>

<body>
  <main>
    <h1>StoffCore Free TV Status</h1>

    <p id="run-time" class="muted">Loading report...</p>

    <section class="cards">
      <div class="card">
        <div>Total channels tested</div>
        <div id="total-channels" class="number">-</div>
      </div>

      <div class="card">
        <div>Working channels</div>
        <div id="working-channels" class="number good">-</div>
      </div>

      <div class="card">
        <div>Broken channels</div>
        <div id="broken-channels" class="number bad">-</div>
      </div>

      <div class="card">
        <div>Success rate</div>
        <div id="success-rate" class="number">-</div>
      </div>

      <div class="card">
        <div>EPG channels</div>
        <div id="epg-channels" class="number">-</div>
      </div>
    </section>

    <section>
      <h2>Dispatcharr URLs</h2>

      <p>
        M3U:
        <code>https://stoffbergsaalih.github.io/stoffcore-free-tv/working.m3u</code>
      </p>

      <p>
        EPG:
        <code>https://stoffbergsaalih.github.io/stoffcore-free-tv/epg.xml.gz</code>
      </p>

      <p>
        <a href="working.m3u">working.m3u</a>
        <a href="epg.xml.gz">epg.xml.gz</a>
        <a href="broken.txt">broken.txt</a>
        <a href="data/check.log">check.log</a>
        <a href="data/report.json">report.json</a>
      </p>
    </section>

    <section>
      <h2>Group stats</h2>

      <table>
        <thead>
          <tr>
            <th>Group</th>
            <th>Total</th>
            <th>Working</th>
            <th>Broken</th>
            <th>Success</th>
          </tr>
        </thead>
        <tbody id="group-stats"></tbody>
      </table>
    </section>

    <section>
      <h2>EPG sources</h2>

      <table>
        <thead>
          <tr>
            <th>Status</th>
            <th>URL</th>
            <th>Channels</th>
            <th>Programmes</th>
            <th>Error</th>
          </tr>
        </thead>
        <tbody id="epg-sources"></tbody>
      </table>
    </section>

    <section>
      <h2>Broken channels</h2>
      <p class="muted">Showing first 200 broken channels. Full list is in broken.txt and report.json.</p>

      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Group</th>
            <th>Error</th>
            <th>URL</th>
          </tr>
        </thead>
        <tbody id="broken-list"></tbody>
      </table>
    </section>
  </main>

  <script src="assets/app.js"></script>
</body>
</html>
""",
        encoding="utf-8",
    )

    STYLE_CSS.write_text(
        """body {
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  margin: 0;
  background: #111827;
  color: #f9fafb;
}

main {
  max-width: 1200px;
  margin: 0 auto;
  padding: 24px;
}

a {
  color: #93c5fd;
  margin-right: 12px;
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 16px;
  margin: 24px 0;
}

.card {
  background: #1f2937;
  border: 1px solid #374151;
  border-radius: 14px;
  padding: 16px;
}

.number {
  font-size: 32px;
  font-weight: 800;
  margin-top: 8px;
}

table {
  width: 100%;
  border-collapse: collapse;
  margin: 16px 0 32px;
  background: #1f2937;
  border-radius: 12px;
  overflow: hidden;
}

th,
td {
  padding: 10px 12px;
  border-bottom: 1px solid #374151;
  text-align: left;
  vertical-align: top;
}

th {
  background: #111827;
}

code {
  color: #e5e7eb;
  word-break: break-word;
}

.good {
  color: #86efac;
}

.bad {
  color: #fca5a5;
}

.muted {
  color: #9ca3af;
}
""",
        encoding="utf-8",
    )

    APP_JS.write_text(
        """async function loadReport() {
  const response = await fetch("data/report.json", { cache: "no-store" });
  const report = await response.json();

  document.getElementById("run-time").innerText =
    `Started: ${report.started_at} | Finished: ${report.finished_at}`;

  document.getElementById("total-channels").innerText = report.total_channels;
  document.getElementById("working-channels").innerText = report.working_channels;
  document.getElementById("broken-channels").innerText = report.broken_channels;
  document.getElementById("success-rate").innerText = `${report.success_rate_percent}%`;
  document.getElementById("epg-channels").innerText = report.epg.channel_count;

  const groupStats = document.getElementById("group-stats");

  Object.entries(report.group_stats)
    .sort(([a], [b]) => a.localeCompare(b))
    .forEach(([group, stats]) => {
      const success = stats.total > 0
        ? ((stats.working / stats.total) * 100).toFixed(2)
        : "0.00";

      groupStats.insertAdjacentHTML("beforeend", `
        <tr>
          <td>${escapeHtml(group)}</td>
          <td>${stats.total}</td>
          <td>${stats.working}</td>
          <td>${stats.broken}</td>
          <td>${success}%</td>
        </tr>
      `);
    });

  const epgSources = document.getElementById("epg-sources");

  report.epg.sources.forEach((source) => {
    epgSources.insertAdjacentHTML("beforeend", `
      <tr>
        <td class="${source.ok ? "good" : "bad"}">${source.ok ? "OK" : "FAILED"}</td>
        <td><code>${escapeHtml(source.url)}</code></td>
        <td>${source.channels}</td>
        <td>${source.programmes}</td>
        <td><code>${escapeHtml(source.error || "")}</code></td>
      </tr>
    `);
  });

  const brokenList = document.getElementById("broken-list");

  report.broken.slice(0, 200).forEach((item) => {
    brokenList.insertAdjacentHTML("beforeend", `
      <tr>
        <td>${escapeHtml(item.name)}</td>
        <td>${escapeHtml(item.group_title || "Ungrouped")}</td>
        <td><code>${escapeHtml(item.error || "")}</code></td>
        <td><a href="${escapeAttribute(item.url)}">stream</a></td>
      </tr>
    `);
  });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value);
}

loadReport().catch((error) => {
  document.body.insertAdjacentHTML(
    "beforeend",
    `<pre>Failed to load report: ${escapeHtml(error)}</pre>`
  );
});
""",
        encoding="utf-8",
    )


def main():
    started_at = now_iso()

    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    CHECK_LOG.write_text("", encoding="utf-8")

    section("🚀 Starting StoffCore Free TV check")

    console(f"Timeout per stream: {TIMEOUT_SECONDS}s")
    console(f"Max workers: {MAX_WORKERS}")

    entries = build_source_playlist()

    section("🔍 Testing streams")
    console(f"Testing {len(entries)} channels with {MAX_WORKERS} workers...")

    results = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(test_stream, entry) for entry in entries]

        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            item = future.result()
            results.append(item)

            status_icon = "✅" if item["ok"] else "❌"
            status_text = "OK" if item["ok"] else "BAD"

            console(
                f"{status_icon} [{index:03}/{len(entries):03}] "
                f"{item['name']} | {item['group_title']} | "
                f"{item['duration_ms']}ms | {status_text}"
            )

            if not item["ok"]:
                console(f"    Error: {item['error']}")

            if index % 25 == 0 or index == len(entries):
                working_count = len([result for result in results if result["ok"]])
                broken_count = len(results) - working_count

                console(
                    f"📊 Progress: {index}/{len(entries)} | "
                    f"working={working_count} broken={broken_count}"
                )

    epg_report = build_epg()

    finished_at = now_iso()

    write_outputs(results, epg_report, started_at, finished_at)

    working_count = len([item for item in results if item["ok"]])
    broken_count = len(results) - working_count
    success_rate = round((working_count / len(results)) * 100, 2) if results else 0

    section("✅ Finished")
    console(f"Total channels: {len(results)}")
    console(f"Working channels: {working_count}")
    console(f"Broken channels: {broken_count}")
    console(f"Success rate: {success_rate}%")
    console("")
    console("GitHub Pages files:")
    console("- public/index.html")
    console("- public/working.m3u")
    console("- public/epg.xml.gz")
    console("- public/broken.txt")
    console("- public/data/report.json")
    console("- public/data/check.log")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        console(f"❌ Fatal error: {error}")
        sys.exit(1)

#!/usr/bin/env python3

import concurrent.futures
import gzip
import shutil
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SOURCES_DIR = ROOT / "sources"
OUTPUT_DIR = ROOT / "output"

PLAYLISTS_FILE = SOURCES_DIR / "playlists.txt"
EPGS_FILE = SOURCES_DIR / "epgs.txt"

SOURCE_M3U = OUTPUT_DIR / "source.m3u"
WORKING_M3U = OUTPUT_DIR / "working.m3u"
BROKEN_TXT = OUTPUT_DIR / "broken.txt"
EPG_XML = OUTPUT_DIR / "epg.xml"
EPG_XML_GZ = OUTPUT_DIR / "epg.xml.gz"

TIMEOUT_SECONDS = 10
MAX_WORKERS = 20


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
        headers={
            "User-Agent": "Mozilla/5.0 free-jellyfin-iptv-checker"
        },
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        data = response.read()

    if url.endswith(".gz"):
        data = gzip.decompress(data)

    return data.decode("utf-8", errors="ignore")


def download_binary(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 free-jellyfin-iptv-checker"
        },
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


def test_stream(entry):
    extinf, url = entry
    name = channel_name(extinf)

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

        output = result.stdout.lower()

        if result.returncode == 0 and ("video" in output or "audio" in output):
            return True, extinf, url, name, ""

        return False, extinf, url, name, result.stderr.strip()[:300]

    except Exception as error:
        return False, extinf, url, name, str(error)


def build_source_playlist():
    playlist_urls = read_url_list(PLAYLISTS_FILE)

    if not playlist_urls:
        raise RuntimeError(f"No playlist URLs found in {PLAYLISTS_FILE}")

    all_entries = []
    seen_urls = set()

    for url in playlist_urls:
        print(f"Downloading playlist: {url}")
        try:
            text = download_text(url)
            entries = parse_m3u(text)

            for extinf, stream_url in entries:
                if stream_url in seen_urls:
                    continue
                seen_urls.add(stream_url)
                all_entries.append((extinf, stream_url))

        except Exception as error:
            print(f"Failed to download playlist {url}: {error}")

    SOURCE_M3U.write_text(
        "#EXTM3U\n"
        + "\n".join([f"{extinf}\n{url}" for extinf, url in all_entries])
        + "\n",
        encoding="utf-8",
    )

    return all_entries


def build_epg():
    epg_urls = read_url_list(EPGS_FILE)

    if not epg_urls:
        print("No EPG URLs configured.")
        return

    print("Building merged EPG...")

    channels = []
    programmes = []

    for url in epg_urls:
        print(f"Downloading EPG: {url}")

        try:
            xml = download_binary(url).decode("utf-8", errors="ignore")
        except Exception as error:
            print(f"Failed to download EPG {url}: {error}")
            continue

        for line in xml.splitlines():
            clean = line.strip()

            if clean.startswith("<channel "):
                channels.append(clean)
            elif clean.startswith("<programme "):
                programmes.append(clean)
            elif programmes and not clean.startswith("<tv") and not clean.startswith("</tv>"):
                programmes.append(clean)

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

    print(f"EPG saved: {EPG_XML_GZ}")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    entries = build_source_playlist()
    print(f"Found {len(entries)} unique channels. Testing...")

    working = []
    broken = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(test_stream, entry) for entry in entries]

        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            ok, extinf, url, name, error = future.result()

            if ok:
                working.append((extinf, url))
                print(f"[OK] {name}")
            else:
                broken.append((name, url, error))
                print(f"[BAD] {name}")

            if index % 25 == 0:
                print(
                    f"Progress: {index}/{len(entries)} | "
                    f"Working: {len(working)} | Broken: {len(broken)}"
                )

    with open(WORKING_M3U, "w", encoding="utf-8") as file:
        file.write("#EXTM3U\n")
        for extinf, url in working:
            file.write(extinf + "\n")
            file.write(url + "\n")

    with open(BROKEN_TXT, "w", encoding="utf-8") as file:
        for name, url, error in broken:
            file.write(f"{name}\n{url}\n{error}\n\n")

    build_epg()

    print()
    print("Done.")
    print(f"Working channels: {len(working)}")
    print(f"Broken channels: {len(broken)}")
    print(f"Saved: {WORKING_M3U}")
    print(f"Saved: {BROKEN_TXT}")
    print(f"Saved: {EPG_XML_GZ}")


if __name__ == "__main__":
    main()

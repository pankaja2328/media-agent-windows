import asyncio
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, unquote, urlparse
from zipfile import ZipFile

from telethon import TelegramClient
from media_agent.credentials import load_credentials


QUALITY = "720"
SUPPORTED_QUALITIES = {"720", "1080", "2160"}
DOWNLOAD_CHUNK_KB = 512
PARALLEL_DOWNLOADS = 3
SINGLE_FILE_PARTS = 3
_PROGRESS_LINE_LENGTH = 0


def safe_name(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:100] or "video"


def format_eta(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def render_progress_line(line):
    """Render in one physical terminal row without wrapping."""
    global _PROGRESS_LINE_LENGTH
    columns = max(40, shutil.get_terminal_size((120, 20)).columns)
    maximum = columns - 1
    visible = line[:maximum]
    padded_length = min(max(_PROGRESS_LINE_LENGTH, len(visible)), maximum)
    sys.stdout.write("\r" + visible.ljust(padded_length))
    sys.stdout.flush()
    _PROGRESS_LINE_LENGTH = len(visible)


def finish_progress_line():
    global _PROGRESS_LINE_LENGTH
    sys.stdout.write("\n")
    sys.stdout.flush()
    _PROGRESS_LINE_LENGTH = 0


def build_progress_line(percent, current_mb, total_mb, speed_mb, eta):
    metrics = (
        f"{percent:6.2f}% {current_mb:,.1f}/{total_mb:,.1f} MB "
        f"{speed_mb:,.2f} MB/s ETA {eta}"
    )
    columns = max(40, shutil.get_terminal_size((120, 20)).columns)
    bar_width = max(5, min(28, columns - len(metrics) - 4))
    filled = min(bar_width, int(bar_width * percent / 100))
    bar = "#" * filled + "-" * (bar_width - filled)
    return f"[{bar}] {metrics}"


def load_api_hash():
    return load_credentials()["telegram_api_hash"]


def series_matches(text, series_name, season):
    """Require the requested show name to occur before Sxx, avoiding title false positives."""
    normalized = re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()
    season_match = re.search(rf"\bS0*{season}(?=E|\b)", normalized)
    expected = re.sub(r"[^A-Z0-9]+", " ", series_name.upper()).strip()
    return bool(season_match and expected and expected in normalized[: season_match.start()])


class DownloadProgress:
    def __init__(self, name):
        self.name = name
        self.started = time.monotonic()
        self.last_print = 0.0
        self.finished = False

    def __call__(self, current, total):
        now = time.monotonic()
        if current < total and now - self.last_print < 0.5:
            return
        self.last_print = now
        elapsed = max(now - self.started, 0.001)
        rate = current / elapsed
        percent = (current / total * 100) if total else 0
        current_mb = current / (1024 * 1024)
        total_mb = total / (1024 * 1024) if total else 0
        speed_mb = rate / (1024 * 1024)
        remaining = max(total - current, 0) / rate if rate and total else 0
        eta = format_eta(remaining)
        line = build_progress_line(percent, current_mb, total_mb, speed_mb, eta)
        render_progress_line(line)
        if total and current >= total:
            finish_progress_line()
            self.finished = True

    def finish(self):
        if not self.finished:
            finish_progress_line()


class BatchDownloadProgress:
    def __init__(self, files):
        self.started = time.monotonic()
        self.last_print = 0.0
        self.state = {
            key: {"current": 0, "total": total} for key, total in files
        }
        self.finished = False

    def callback(self, key):
        def update(current, total):
            entry = self.state[key]
            entry["current"] = current
            entry["total"] = total or entry["total"]
            now = time.monotonic()
            if now - self.last_print < 0.5 and current < entry["total"]:
                return
            self.last_print = now
            current_all = sum(item["current"] for item in self.state.values())
            total_all = sum(item["total"] for item in self.state.values())
            elapsed = max(now - self.started, 0.001)
            rate = current_all / elapsed
            percent = current_all / total_all * 100 if total_all else 0
            remaining = max(total_all - current_all, 0) / rate if rate else 0
            eta = format_eta(remaining)
            line = build_progress_line(
                percent,
                current_all / (1024 * 1024),
                total_all / (1024 * 1024),
                rate / (1024 * 1024),
                eta,
            )
            render_progress_line(line)
            if total_all and current_all >= total_all:
                finish_progress_line()
                self.finished = True

        return update

    def finish(self):
        if not self.finished:
            finish_progress_line()


async def download_message_file(
    client, message, destination, progress_callback, parallel_parts=1
):
    """Download one Telegram document with 512 KB chunks and optional ranged workers."""
    destination = Path(destination)
    file_size = int(message.file.size or 0)
    chunk_size = DOWNLOAD_CHUNK_KB * 1024
    total_chunks = (file_size + chunk_size - 1) // chunk_size if file_size else 0
    workers = min(max(1, parallel_parts), max(1, total_chunks))

    if workers == 1:
        await client.download_file(
            message.document,
            file=str(destination),
            part_size_kb=DOWNLOAD_CHUNK_KB,
            file_size=file_size,
            progress_callback=progress_callback,
        )
        return str(destination) if destination.exists() else None

    with destination.open("wb") as output:
        output.truncate(file_size)

    downloaded_total = 0

    async def download_range(worker_index):
        nonlocal downloaded_total
        start_chunk = total_chunks * worker_index // workers
        end_chunk = total_chunks * (worker_index + 1) // workers
        offset = start_chunk * chunk_size
        limit = end_chunk - start_chunk
        position = offset
        with destination.open("r+b", buffering=0) as output:
            async for chunk in client.iter_download(
                message.document,
                offset=offset,
                limit=limit,
                chunk_size=chunk_size,
                request_size=chunk_size,
                file_size=file_size,
            ):
                output.seek(position)
                output.write(chunk)
                position += len(chunk)
                downloaded_total += len(chunk)
                progress_callback(min(downloaded_total, file_size), file_size)

    try:
        await asyncio.gather(*(download_range(index) for index in range(workers)))
        if downloaded_total < file_size:
            raise RuntimeError(
                f"Incomplete ranged download: {downloaded_total}/{file_size} bytes"
            )
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return str(destination) if destination.exists() else None


def episode_numbers(text, season):
    """Return episode numbers mentioned for the requested season."""
    normalized = re.sub(r"[._-]+", " ", text.upper())
    season_match = re.search(r"\bS0*(\d{1,2})(?=E|\b)", normalized)
    if not season_match or int(season_match.group(1)) != season:
        return set()
    return {int(value) for value in re.findall(r"E0*(\d{1,3})(?=E|\b)", normalized)}


def telegram_start_command(url):
    """Convert a Telegram deep link into (bot username, /start command)."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if parsed.scheme in {"http", "https"} and parsed.netloc.lower() in {
        "t.me",
        "telegram.me",
        "www.t.me",
    }:
        username = parsed.path.strip("/").split("/")[0]
    elif parsed.scheme == "tg" and parsed.netloc == "resolve":
        username = query.get("domain", [""])[0]
    else:
        return None
    if not username or username.startswith("+"):
        return None
    payload = query.get("start", query.get("startapp", [""]))[0]
    command = "/start" + (f" {unquote(payload)}" if payload else "")
    return username, command


async def activate_button(client, message, button):
    """Click a callback button or safely follow a Telegram bot deep link."""
    if button.url:
        start = telegram_start_command(button.url)
        if not start:
            return False
        username, command = start
        await client.send_message(username, command)
        return True
    if button.data:
        # Result pages are edited in place. Refetch immediately before clicking
        # so callback data from the previous page is not reused.
        fresh = await client.get_messages(message.chat_id, ids=message.id)
        if not fresh:
            return False
        answer = await fresh.click(text=lambda value: value == button.text)
        callback_url = getattr(answer, "url", None)
        if callback_url:
            start = telegram_start_command(callback_url)
            if start:
                username, command = start
                await client.send_message(username, command)
        return True
    return False


async def new_private_messages(client, before):
    messages = []
    async for dialog in client.iter_dialogs():
        if not dialog.is_user or not dialog.message:
            continue
        min_id = before.get(dialog.id, 0)
        if dialog.message.id <= min_id:
            continue
        async for message in client.iter_messages(dialog.entity, min_id=min_id, limit=100):
            messages.append(message)
    messages.sort(key=lambda item: (item.date, item.id))
    return messages


async def main():
    settings = load_credentials()
    api_hash = load_api_hash()
    title = input("Title (for example Strike Back S01): ").strip()
    quality = os.environ.get("TELEGRAM_QUALITY", QUALITY).strip().lower().removesuffix("p")
    if quality not in SUPPORTED_QUALITIES:
        raise SystemExit("Quality must be 720p, 1080p, or 2160p.")
    if not title:
        raise SystemExit("Enter a title such as Strike Back S01.")

    quality_pattern = re.compile(rf"(?<!\d){quality}p?(?![a-z\d])", re.I)
    season_match = re.search(r"\bS0*(\d{1,2})(?=E|\b)", title, re.I)
    if not season_match:
        raise SystemExit("Include the season in the title, for example Strike Back S01.")
    season = int(season_match.group(1))
    series_name = re.sub(
        r"\b(?:19|20)\d{2}\b", "", title[: season_match.start()], flags=re.I
    ).strip(" ()-._")
    session = Path(__file__).with_name("telegram_zip")
    async with TelegramClient(
        str(session), int(settings["telegram_api_id"]), api_hash
    ) as client:
        # Keep a per-chat high-water mark so files delivered to a different PM
        # can be found without accidentally collecting older Telegram files.
        before = {}
        async for dialog in client.iter_dialogs():
            if dialog.is_user:
                before[dialog.id] = dialog.message.id if dialog.message else 0

        await client.send_message(settings["media_bot_username"], title)
        print(
            "Searching every results page and following delivery buttons automatically. "
            "This can take several minutes."
        )

        acted = set()
        requested_episodes = set()
        documents = {}
        started_at = time.monotonic()
        last_activity = started_at
        results_finished = False
        last_status = 0.0
        max_wait_seconds = 10 * 60
        while True:
            action_count = 0
            documents_before = len(documents)
            for message in await new_private_messages(client, before):
                text = message.raw_text or ""
                if message.file and not message.out:
                    name = message.file.name or f"video{message.file.ext or ''}"
                    details = f"{name}\n{text}"
                    episodes = episode_numbers(details, season)
                    if (
                        Path(name).suffix.lower() in {".mkv", ".mp4"}
                        and quality_pattern.search(details)
                        and episodes
                        and series_matches(details, series_name, season)
                        and message.document
                    ):
                        documents[message.document.id] = (message, name, episodes)

                button_labels = [
                    item.text or "" for row in (message.buttons or []) for item in row
                ]
                page_counters = [
                    re.search(r"\b(\d+)\s*/\s*(\d+)\b", item) for item in button_labels
                ]
                page_counters = [match for match in page_counters if match]
                is_results_page = bool(page_counters)
                has_next = any(re.search(r"\bnext\b|⏩", item, re.I) for item in button_labels)
                if is_results_page and not has_next:
                    results_finished = True
                for counter in page_counters:
                    if int(counter.group(1)) >= int(counter.group(2)):
                        results_finished = True
                for row in message.buttons or []:
                    for button in row:
                        label = button.text or ""
                        action_id = (
                            message.chat_id,
                            message.id,
                            label,
                            button.url,
                            bytes(button.data) if button.data else None,
                            text,
                        )
                        if action_id in acted:
                            continue

                        label_episodes = episode_numbers(label, season)
                        is_result = (
                            quality_pattern.search(label)
                            and label_episodes
                            and series_matches(label, series_name, season)
                        )
                        is_download = re.search(r"download\s*(?:the\s*)?(?:file|files)", label, re.I)
                        is_next = is_results_page and re.search(r"\bnext\b|⏩", label, re.I)

                        should_click = False
                        if is_result and not label_episodes.issubset(requested_episodes):
                            requested_episodes.update(label_episodes)
                            should_click = True
                            print(f"Selecting: {label}")
                        elif is_download:
                            should_click = True
                            print(f"Following: {label}")
                        elif is_next:
                            should_click = True
                            print(f"Opening next results page: {label}")

                        if should_click:
                            try:
                                if await activate_button(client, message, button):
                                    acted.add(action_id)
                                    action_count += 1
                                    last_activity = time.monotonic()
                                    await asyncio.sleep(1)
                            except Exception as error:
                                print(f"Could not activate {label!r}: {error}")
            now = time.monotonic()
            if len(documents) > documents_before:
                last_activity = now
            if action_count == 0 and now - last_status >= 10:
                page_state = "final page reached" if results_finished else "scanning pages"
                print(
                    f"Status: {page_state}; requested {len(requested_episodes)} episode(s); "
                    f"received {len(documents)} matching file(s)."
                )
                last_status = now

            quiet_for = now - last_activity
            if results_finished and quiet_for >= (30 if documents else 15):
                break
            if now - started_at >= max_wait_seconds:
                print("Stopped after 10 minutes. Processing the files received so far.")
                break
            await asyncio.sleep(2)

        # One output file per episode. A double episode such as S01E01E02
        # covers both episode numbers and is included only once.
        matches = []
        covered = set()
        ordered = sorted(
            documents.values(),
            key=lambda item: (min(item[2]), item[0].file.size or 0),
        )
        for message, name, episodes in ordered:
            if episodes.issubset(covered):
                continue
            matches.append((message, name, episodes))
            covered.update(episodes)

        if not matches:
            print("No matching video files found. Let the delivery bots finish before pressing Enter.")
            return

        print("\nFiles to include")
        for message, name, episodes in matches:
            episode_label = ", ".join(f"S{season:02d}E{ep:02d}" for ep in sorted(episodes))
            print(f"  {episode_label}: {name}")
        print("Detected episodes:", ", ".join(f"E{ep:02d}" for ep in sorted(covered)))
        if input("Download these files? [y/N] ").strip().lower() != "y":
            return

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        default_root = Path(__file__).with_name("media_agent") / "downloads"
        download_root = Path(os.environ.get("TELEGRAM_DOWNLOAD_ROOT", default_root))
        media_folder = download_root / safe_name(title)
        media_folder.mkdir(parents=True, exist_ok=True)
        archive = media_folder / f"{safe_name(title)} - {quality}p - {timestamp}.zip"
        partial = archive.with_suffix(".zip.part")
        try:
            with TemporaryDirectory(dir=media_folder) as scratch, ZipFile(partial, "x") as zipped:
                for offset in range(0, len(matches), PARALLEL_DOWNLOADS):
                    batch = matches[offset : offset + PARALLEL_DOWNLOADS]
                    print(
                        f"\nDownloading {len(batch)} file(s) concurrently "
                        f"with {DOWNLOAD_CHUNK_KB} KB chunks:"
                    )
                    prepared = []
                    for message, name, episodes in batch:
                        filename = (
                            f"{message.id} - {safe_name(Path(name).stem)}{Path(name).suffix}"
                        )
                        destination = Path(scratch) / filename
                        label = ",".join(f"E{episode:02d}" for episode in sorted(episodes))
                        prepared.append((message, name, filename, destination, label))
                        print(f"  {label}: {name}")

                    progress = BatchDownloadProgress(
                        [(label, message.file.size or 0) for message, _, _, _, label in prepared]
                    )

                    async def fetch(item):
                        message, name, filename, destination, label = item
                        downloaded = await download_message_file(
                            client,
                            message,
                            destination,
                            progress.callback(label),
                            parallel_parts=(SINGLE_FILE_PARTS if len(batch) == 1 else 1),
                        )
                        if not downloaded:
                            raise RuntimeError(f"Could not download {name}")
                        return downloaded, filename

                    downloaded_batch = await asyncio.gather(
                        *(fetch(item) for item in prepared)
                    )
                    progress.finish()
                    print("Adding completed batch to ZIP...")
                    for downloaded, filename in downloaded_batch:
                        zipped.write(downloaded, arcname=filename)
                        Path(downloaded).unlink()
            partial.rename(archive)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        print(f"\nZIP ready at {archive.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())

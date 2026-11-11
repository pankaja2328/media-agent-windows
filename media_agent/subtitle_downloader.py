import asyncio
import importlib.util
import re
import shutil
import zipfile
from pathlib import Path

from telethon import TelegramClient

from .progress import emit_progress
from .credentials import load_credentials


PROJECT_DIR = Path(__file__).resolve().parents[1]
LEGACY_DIR = PROJECT_DIR
LEGACY_SCRIPT = LEGACY_DIR / "telegram_zip.py"
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".zip"}
EXTRACTABLE_SUBTITLE_EXTENSIONS = SUBTITLE_EXTENSIONS - {".zip"}
MAX_ARCHIVE_FILES = 200
MAX_SUBTITLE_SIZE = 20 * 1024 * 1024
MAX_ARCHIVE_OUTPUT = 50 * 1024 * 1024


def _load_legacy():
    spec = importlib.util.spec_from_file_location("legacy_subtitle_telegram", LEGACY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize(value):
    return re.sub(r"[^A-Z0-9]+", " ", value.upper()).strip()


def _matches_title(label, title):
    expected = _normalize(title)
    actual = _normalize(label)
    return expected in actual or all(word in actual for word in expected.split())


def _candidate_score(label, title, year, season, preferred_quality):
    suffix = Path(label.rsplit(" - ", 1)[-1].strip()).suffix.lower()
    if suffix not in SUBTITLE_EXTENSIONS or not _matches_title(label, title):
        return -1
    mentioned_years = set(re.findall(r"\b(?:19|20)\d{2}\b", label))
    if year and mentioned_years and str(year) not in mentioned_years:
        return -1
    mentioned_seasons = set(re.findall(r"\bS0*(\d{1,2})(?:\b|E)", label, re.I))
    if season and mentioned_seasons and str(season) not in {
        str(int(value)) for value in mentioned_seasons
    }:
        return -1
    score = 10
    if year and str(year) in label:
        score += 5
    if season and re.search(rf"\bS0*{season}(?:\b|E)", label, re.I):
        score += 5
    if preferred_quality and preferred_quality.lower() in label.lower():
        score += 3
    if suffix in {".srt", ".ass", ".ssa", ".vtt"}:
        score += 2
    return score


def _available_path(folder, filename):
    path = folder / filename
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = folder / f"{path.stem} ({index}){path.suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not choose a unique subtitle filename for {filename}")


def _extract_subtitle_archive(archive, destination_dir, safe_name) -> list[Path]:
    """Safely extract only subtitle files from a downloaded ZIP archive."""
    archive = Path(archive)
    destination_dir = Path(destination_dir).resolve()
    extracted = []
    total_output = 0
    try:
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if len(members) > MAX_ARCHIVE_FILES:
                print(f"Skipping subtitle ZIP with too many entries: {archive.name}")
                return []
            for member in members:
                member_name = Path(member.filename.replace("\\", "/")).name
                suffix = Path(member_name).suffix.casefold()
                if (
                    member.is_dir()
                    or not member_name
                    or suffix not in EXTRACTABLE_SUBTITLE_EXTENSIONS
                    or member.flag_bits & 0x1
                    or member.file_size <= 0
                    or member.file_size > MAX_SUBTITLE_SIZE
                    or total_output + member.file_size > MAX_ARCHIVE_OUTPUT
                ):
                    continue
                filename = safe_name(member_name)
                target = _available_path(destination_dir, filename)
                # Using only the basename prevents ZIP path traversal. The
                # resolved-parent check provides an additional containment guard.
                if target.resolve().parent != destination_dir:
                    continue
                with source.open(member) as input_file, target.open("wb") as output_file:
                    shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                total_output += target.stat().st_size
                extracted.append(target.resolve())
                print(f"Extracted Sinhala subtitle: {target.resolve()}")
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        print(f"Could not extract subtitle ZIP {archive.name}: {error}")
    return extracted


async def _send_query_with_retry(client, bot, query, attempts=3) -> bool:
    """Retry transient Telegram send failures without failing the movie download."""
    for attempt in range(1, attempts + 1):
        try:
            await client.send_message(bot, query)
            return True
        except Exception as error:
            if attempt >= attempts:
                print(f"Telegram subtitle search failed after {attempts} attempts: {error}")
                return False
            delay = min(20, max(2, int(getattr(error, "seconds", 0) or 2**attempt)))
            print(f"Subtitle search connection problem; retrying in {delay}s...")
            emit_progress(
                "subtitles",
                message=f"Telegram connection interrupted — retrying in {delay} seconds",
            )
            await asyncio.sleep(delay)
    return False


async def download_sinhala_subtitle(
    client,
    title,
    destination_dir,
    year=None,
    season=None,
    preferred_quality="720p",
):
    legacy = _load_legacy()
    settings = load_credentials()
    subtitle_bot = settings["subtitle_bot_username"]
    bot = None
    latest = []
    for attempt in range(1, 4):
        try:
            bot = await client.get_entity(subtitle_bot)
            latest = await client.get_messages(bot, limit=1)
            break
        except Exception as error:
            if attempt == 3:
                print(f"Could not connect to the Sinhala subtitle bot: {error}")
                emit_progress(
                    "subtitle_missing",
                    title=title,
                    quality=preferred_quality,
                    message="Subtitle bot is unavailable; the movie is safe and subtitles can be retried later",
                )
                return []
            delay = 2**attempt
            print(f"Connecting to subtitle bot failed; retrying in {delay}s...")
            await asyncio.sleep(delay)
    baseline = latest[0].id if latest else 0
    query = f"{title} {year or ''}".strip()
    if season:
        query = f"{title} S{season:02d}"
    if not await _send_query_with_retry(client, bot, query):
        emit_progress(
            "subtitle_missing",
            title=title,
            quality=preferred_quality,
            message="Telegram could not be reached; the movie is safe and subtitles can be retried later",
        )
        return []
    print(f"Searching Sinhala subtitles: {query}")
    emit_progress(
        "subtitles",
        title=title,
        quality=preferred_quality,
        message=f"Searching Sinhala subtitles: {query}",
    )

    acted = set()
    selected_count = 0
    results_finished = False
    delivered = {}
    loop = asyncio.get_running_loop()
    started = loop.time()
    last_activity = started
    fallback_queries = []
    simple_query = title.strip()
    if simple_query.casefold() != query.casefold():
        fallback_queries.append(simple_query)
    # A final repeat helps when the subtitle bot was temporarily busy and did
    # not answer the original request.
    fallback_queries.append(query)
    next_fallback_at = 45.0

    while loop.time() - started < 2 * 60:
        elapsed = loop.time() - started
        if not delivered and fallback_queries and elapsed >= next_fallback_at:
            fallback_query = fallback_queries.pop(0)
            if await _send_query_with_retry(client, bot, fallback_query, attempts=2):
                print(f"Retrying Sinhala subtitle search: {fallback_query}")
                emit_progress(
                    "subtitles",
                    title=title,
                    quality=preferred_quality,
                    message=f"No result yet — retrying with: {fallback_query}",
                )
            next_fallback_at += 45.0

        messages = []
        try:
            async for message in client.iter_messages(bot, min_id=baseline, limit=100):
                messages.append(message)
        except Exception as error:
            print(f"Telegram subtitle response timed out; continuing: {error}")
            emit_progress(
                "subtitles",
                title=title,
                quality=preferred_quality,
                message="Telegram response timed out — reconnecting automatically",
            )
            await asyncio.sleep(3)
            continue
        messages.sort(key=lambda item: item.id)

        candidates = []
        next_buttons = []
        page_counters = []
        for message in messages:
            if message.file and not message.out:
                name = message.file.name or "subtitle"
                if (
                    Path(name).suffix.lower() in SUBTITLE_EXTENSIONS
                    and message.document.id not in delivered
                ):
                    delivered[message.document.id] = (message, name)
                    last_activity = loop.time()

            for row in message.buttons or []:
                for button in row:
                    label = button.text or ""
                    key = (
                        message.id,
                        label,
                        button.url,
                        bytes(button.data) if button.data else None,
                        message.raw_text or "",
                    )
                    if key in acted:
                        continue
                    score = _candidate_score(
                        label, title, year, season, preferred_quality
                    )
                    if score >= 0:
                        candidates.append((score, message, button, key, label))
                    elif re.search(r"\bnext\b|⏩", label, re.I):
                        next_buttons.append((message, button, key, label))
                    counter = re.search(r"\b(\d+)\s*/\s*(\d+)\b", label)
                    if counter:
                        page_counters.append(counter)

        if page_counters and any(
            int(counter.group(1)) >= int(counter.group(2)) for counter in page_counters
        ):
            results_finished = True
        elif page_counters and not next_buttons:
            results_finished = True

        if results_finished and delivered and loop.time() - last_activity >= 3:
            break

        if candidates:
            for _, message, button, key, label in sorted(
                candidates, key=lambda item: item[0], reverse=True
            ):
                if await legacy.activate_button(client, message, button):
                    acted.add(key)
                    selected_count += 1
                    last_activity = loop.time()
                    print(f"Selected Sinhala subtitle: {label}")
                    await asyncio.sleep(0.5)
        if next_buttons and not results_finished:
            message, button, key, label = next_buttons[-1]
            if await legacy.activate_button(client, message, button):
                acted.add(key)
                last_activity = loop.time()
                await asyncio.sleep(1)

        await asyncio.sleep(2)

    if not delivered:
        emit_progress(
            "subtitle_missing",
            title=title,
            quality=preferred_quality,
            message="No Sinhala subtitle was found; the movie download is complete and subtitles can be retried later",
        )
        return []

    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    downloaded_paths = []
    seen_names = set()
    for message, name in delivered.values():
        safe_filename = legacy.safe_name(name)
        normalized_name = safe_filename.casefold()
        if normalized_name in seen_names:
            continue
        seen_names.add(normalized_name)
        destination = _available_path(destination_dir, safe_filename)
        downloaded = await legacy.download_message_file(
            client,
            message,
            destination,
            lambda current, total: None,
            parallel_parts=1,
        )
        if downloaded:
            downloaded_path = Path(downloaded).resolve()
            print(f"Sinhala subtitle saved: {downloaded_path}")
            if downloaded_path.suffix.casefold() == ".zip":
                extracted = _extract_subtitle_archive(
                    downloaded_path, destination_dir, legacy.safe_name
                )
                downloaded_paths.extend(str(path) for path in extracted)
            else:
                downloaded_paths.append(str(downloaded_path))
    return downloaded_paths


def download_sinhala_subtitle_sync(
    title,
    destination_dir,
    year=None,
    season=None,
    preferred_quality="720p",
):
    async def run():
        legacy = _load_legacy()
        settings = load_credentials()
        session = LEGACY_DIR / "telegram_zip"
        async with TelegramClient(
            str(session), int(settings["telegram_api_id"]), settings["telegram_api_hash"]
        ) as client:
            return await download_sinhala_subtitle(
                client,
                title,
                destination_dir,
                year=year,
                season=season,
                preferred_quality=preferred_quality,
            )

    return asyncio.run(run())

import asyncio
import importlib.util
import math
import re
import time
from pathlib import Path

from telethon import TelegramClient

from .subtitle_downloader import download_sinhala_subtitle
from .subtitle_sync import extract_embedded_english_subtitle, select_and_sync_subtitles
from .subtitle_translate import translate_english_srt_to_sinhala
from .progress import emit_progress
from .credentials import load_credentials


PROJECT_DIR = Path(__file__).resolve().parents[1]
LEGACY_DIR = PROJECT_DIR
LEGACY_SCRIPT = LEGACY_DIR / "telegram_zip.py"
DOWNLOAD_DIR = PROJECT_DIR / "downloads"


def _load_legacy():
    if not LEGACY_SCRIPT.exists():
        raise FileNotFoundError(f"Existing Telegram downloader not found: {LEGACY_SCRIPT}")
    spec = importlib.util.spec_from_file_location("legacy_telegram_zip", LEGACY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _title_matches(text: str, title: str) -> bool:
    normalize = lambda value: re.sub(r"[^A-Z0-9]+", " ", value.upper()).strip()
    return normalize(title) in normalize(text)


def _size_from_text(text: str) -> int:
    match = re.search(r"\b([\d,.]+)\s*(GB|MB)\b", text, re.I)
    if not match:
        return 0
    amount = float(match.group(1).replace(",", ""))
    return int(amount * (1024**3 if match.group(2).upper() == "GB" else 1024**2))


def _release_quality_score(text: str, preferred_quality: str, size: int = 0) -> int:
    """Rank releases at the requested resolution without treating smallest as best."""
    value = re.sub(r"[._-]+", " ", text).casefold()
    score = 0
    requested = preferred_quality.casefold().removesuffix("p")
    resolution = re.search(r"\b(480|576|720|1080|2160)p?\b", value)
    if resolution:
        score += 60 if resolution.group(1) == requested else -80

    source_scores = (
        (r"\b(?:remux)\b", 45),
        (r"\b(?:blu ?ray|brrip|bdrip)\b", 38),
        (r"\b(?:web ?dl)\b", 32),
        (r"\b(?:web ?rip)\b", 25),
        (r"\b(?:hdtv)\b", 15),
        (r"\b(?:dvdrip|dvd)\b", 8),
    )
    for pattern, points in source_scores:
        if re.search(pattern, value):
            score += points
            break

    if re.search(r"\b(?:cam|camrip|hdcam|telesync|telecine|tsrip)\b", value):
        score -= 150
    if re.search(r"\b(?:x265|h ?265|hevc|av1)\b", value):
        score += 5
    elif re.search(r"\b(?:x264|h ?264|avc)\b", value):
        score += 3
    if re.search(r"\b10 ?bit\b", value):
        score += 3
    if re.search(r"\b(?:hdr10?|dolby vision|dovi)\b", value):
        score += 4
    if re.search(r"\b(?:truehd|dts(?: hd)?|ddp|eac3)\b", value):
        score += 5
    elif re.search(r"\b(?:aac|ac3)\b", value):
        score += 2
    if re.search(r"\b(?:5[ .]?1|7[ .]?1)\b", value):
        score += 3

    effective_size = size or _size_from_text(text)
    if effective_size:
        size_mb = effective_size / (1024**2)
        score += min(14, max(0, round(math.log2(max(size_mb, 1)) - 7)))
        if requested == "720" and size_mb < 350:
            score -= 8
        elif requested == "1080" and size_mb < 700:
            score -= 8
    return score


async def _download_movie(title: str, year: int | None, quality: str) -> str:
    legacy = _load_legacy()
    query = f"{title} {year or ''}".strip()
    quality_number = quality.lower().removesuffix("p")
    quality_pattern = re.compile(rf"(?<!\d){quality_number}p?(?![a-z\d])", re.I)
    session = LEGACY_DIR / "telegram_zip"
    settings = load_credentials()
    async with TelegramClient(
        str(session), int(settings["telegram_api_id"]), settings["telegram_api_hash"]
    ) as client:
        before = {}
        async for dialog in client.iter_dialogs():
            if dialog.is_user:
                before[dialog.id] = dialog.message.id if dialog.message else 0
        await client.send_message(settings["media_bot_username"], query)
        print(f"Searching Telegram for {query} ({quality})...")
        emit_progress(
            "searching",
            title=title,
            year=year,
            quality=quality,
            message="Searching Telegram for the best release",
        )

        acted = set()
        documents = {}
        result_selected = False
        results_finished = False
        loop = asyncio.get_running_loop()
        started = loop.time()
        last_activity = started
        last_status = 0.0

        while loop.time() - started < 8 * 60:
            messages = await legacy.new_private_messages(client, before)
            documents_before = len(documents)
            movie_actions = []
            next_actions = []
            for message in messages:
                text = message.raw_text or ""
                if message.file and not message.out:
                    name = message.file.name or f"movie{message.file.ext or ''}"
                    details = f"{name}\n{text}"
                    if (
                        Path(name).suffix.lower() in {".mkv", ".mp4"}
                        and quality_pattern.search(details)
                        and _title_matches(details, title)
                        and not re.search(r"\bS\d{1,2}E\d{1,3}\b", details, re.I)
                        and message.document
                    ):
                        documents[message.document.id] = (message, name)

                labels = [button.text or "" for row in (message.buttons or []) for button in row]
                counters = [re.search(r"\b(\d+)\s*/\s*(\d+)\b", label) for label in labels]
                counters = [counter for counter in counters if counter]
                has_next = any(re.search(r"\bnext\b|⏩", label, re.I) for label in labels)
                if counters and not has_next:
                    results_finished = True

                for row in message.buttons or []:
                    for button in row:
                        label = button.text or ""
                        action_id = (message.chat_id, message.id, label, button.url, text)
                        if action_id in acted:
                            continue
                        movie_result = (
                            not result_selected
                            and quality_pattern.search(label)
                            and _title_matches(label, title)
                            and not re.search(r"\bS\d{1,2}E\d{1,3}\b", label, re.I)
                            and (not year or str(year) in label)
                        )
                        download_button = re.search(
                            r"download\s*(?:the\s*)?(?:file|files)", label, re.I
                        )
                        delivery_link = re.search(
                            r"files?\s+sent\s+to\s+pm|get\s+files|open\s+(?:the\s+)?bot",
                            label,
                            re.I,
                        )
                        next_button = (
                            not result_selected
                            and bool(counters)
                            and re.search(r"\bnext\b|⏩", label, re.I)
                        )
                        if not (movie_result or download_button or delivery_link or next_button):
                            continue
                        if movie_result:
                            movie_actions.append(
                                (
                                    _release_quality_score(label, quality),
                                    message,
                                    button,
                                    action_id,
                                    label,
                                )
                            )
                            continue
                        if next_button:
                            next_actions.append((message, button, action_id, label))
                            continue
                        try:
                            if await legacy.activate_button(client, message, button):
                                acted.add(action_id)
                                last_activity = loop.time()
                                if download_button or delivery_link:
                                    print(f"Following delivery: {label}")
                                await asyncio.sleep(1)
                        except Exception as error:
                            print(f"Could not activate {label!r}: {error}")

            if movie_actions and not result_selected:
                score, message, button, action_id, label = max(
                    movie_actions, key=lambda item: item[0]
                )
                try:
                    if await legacy.activate_button(client, message, button):
                        acted.add(action_id)
                        result_selected = True
                        last_activity = loop.time()
                        print(f"Selected best release (quality score {score}): {label}")
                        emit_progress(
                            "selected",
                            title=title,
                            year=year,
                            quality=quality,
                            filename=label,
                            quality_score=score,
                            message="Best matching release selected",
                        )
                        await asyncio.sleep(1)
                except Exception as error:
                    print(f"Could not activate {label!r}: {error}")
            elif next_actions and not result_selected:
                message, button, action_id, label = next_actions[-1]
                try:
                    if await legacy.activate_button(client, message, button):
                        acted.add(action_id)
                        last_activity = loop.time()
                        print(f"Opening more results: {label}")
                        await asyncio.sleep(1)
                except Exception as error:
                    print(f"Could not activate {label!r}: {error}")

            if len(documents) > documents_before:
                last_activity = loop.time()
                print(f"Received {len(documents)} matching movie file(s).")
                emit_progress(
                    "received",
                    title=title,
                    quality=quality,
                    count=len(documents),
                    message="Movie file received from Telegram",
                )
            now = loop.time()
            if now - last_status >= 10:
                print(
                    "Status: "
                    f"result_selected={result_selected}, "
                    f"matching_files={len(documents)}."
                )
                last_status = now
            if result_selected and documents and now - last_activity >= 10:
                break
            await asyncio.sleep(2)

        if not documents:
            return "No matching movie file was delivered."
        message, name = max(
            documents.values(),
            key=lambda item: _release_quality_score(
                f"{item[1]}\n{item[0].raw_text or ''}",
                quality,
                item[0].file.size or 0,
            ),
        )
        selected_score = _release_quality_score(
            f"{name}\n{message.raw_text or ''}", quality, message.file.size or 0
        )
        print(f"Downloading best delivered release (quality score {selected_score}): {name}")
        folder_title = f"{title} ({year})" if year else title
        media_folder = DOWNLOAD_DIR / legacy.safe_name(folder_title)
        media_folder.mkdir(parents=True, exist_ok=True)
        destination = media_folder / legacy.safe_name(name)
        terminal_progress = legacy.DownloadProgress(name)
        download_started = time.monotonic()
        last_web_update = 0.0
        total_size = int(message.file.size or 0)
        emit_progress(
            "downloading",
            title=title,
            year=year,
            quality=quality,
            filename=name,
            quality_score=selected_score,
            current=0,
            total=total_size,
            percent=0,
            speed=0,
            eta=None,
            message="Downloading movie",
        )

        def progress(current, total):
            nonlocal last_web_update
            terminal_progress(current, total)
            now = time.monotonic()
            if current < total and now - last_web_update < 0.45:
                return
            last_web_update = now
            elapsed = max(now - download_started, 0.001)
            speed = current / elapsed
            remaining = max(total - current, 0)
            eta = remaining / speed if speed and total else None
            emit_progress(
                "downloading",
                title=title,
                year=year,
                quality=quality,
                filename=name,
                quality_score=selected_score,
                current=current,
                total=total,
                percent=(current / total * 100) if total else 0,
                speed=speed,
                eta=eta,
                message="Downloading movie",
            )

        downloaded = await legacy.download_message_file(
            client,
            message,
            destination,
            progress,
            parallel_parts=legacy.SINGLE_FILE_PARTS,
        )
        terminal_progress.finish()
        if not downloaded:
            emit_progress("error", title=title, message="Telegram download failed")
            return "Telegram delivered a result, but the local download failed."
        movie_path = Path(downloaded).resolve()
        emit_progress(
            "subtitles",
            title=title,
            quality=quality,
            filename=name,
            current=total_size,
            total=total_size,
            percent=100,
            message="Movie complete — finding Sinhala subtitles",
        )
        subtitle_paths = await download_sinhala_subtitle(
            client,
            title,
            movie_path.parent,
            year=year,
            preferred_quality=quality,
        )
        if subtitle_paths:
            listed = "\n".join(f"  - {path}" for path in subtitle_paths)
            sync_results = select_and_sync_subtitles(movie_path.parent)
            selected = [item for item in sync_results if item.get("subtitle")]
            if selected:
                synced = "\n".join(f"  - {item['subtitle']}" for item in selected)
                result = (
                    f"Movie downloaded: {movie_path}\n"
                    f"Sinhala subtitle candidates:\n{listed}\n"
                    f"Audio-synchronized subtitle:\n{synced}"
                )
                emit_progress(
                    "complete",
                    title=title,
                    quality=quality,
                    filename=name,
                    folder=str(movie_path.parent),
                    subtitle_count=len(selected),
                    percent=100,
                    message="Movie and synchronized Sinhala subtitle are ready",
                )
                return result
            emit_progress(
                "subtitles",
                title=title,
                quality=quality,
                filename=name,
                percent=100,
                message="Sinhala candidates did not synchronize — checking embedded English subtitles",
            )
        emit_progress(
            "subtitles",
            title=title,
            quality=quality,
            filename=name,
            percent=100,
            message="No usable Sinhala subtitle — checking the movie for embedded English subtitles",
        )
        english_subtitle = extract_embedded_english_subtitle(movie_path)
        if english_subtitle:
            emit_progress(
                "subtitles",
                title=title,
                quality=quality,
                filename=name,
                percent=100,
                message="English subtitle extracted — translating it to Sinhala with Gemini",
            )

            def translation_progress(current_batch, total_batches):
                emit_progress(
                    "subtitles",
                    title=title,
                    quality=quality,
                    filename=name,
                    percent=100,
                    message=f"Translating subtitles to Sinhala — batch {current_batch} of {total_batches}",
                )

            sinhala_subtitle = translate_english_srt_to_sinhala(
                english_subtitle,
                movie_path.with_suffix(".srt"),
                progress_callback=translation_progress,
            )
            if sinhala_subtitle:
                emit_progress(
                    "complete",
                    title=title,
                    quality=quality,
                    filename=name,
                    folder=str(movie_path.parent),
                    subtitle_count=2,
                    percent=100,
                    message="Movie and Gemini-translated Sinhala subtitle are ready",
                )
                return (
                    f"Movie downloaded: {movie_path}\n"
                    f"Embedded English subtitle: {english_subtitle}\n"
                    f"Gemini-translated Sinhala subtitle: {sinhala_subtitle}"
                )
            emit_progress(
                "complete",
                title=title,
                quality=quality,
                filename=name,
                folder=str(movie_path.parent),
                subtitle_count=1,
                percent=100,
                message="English subtitle is ready; Sinhala translation could not be completed",
            )
            return (
                f"Movie downloaded: {movie_path}\n"
                f"Sinhala subtitles: no matching files found\n"
                f"Embedded English subtitle extracted: {english_subtitle}\n"
                "Automatic Sinhala translation could not be completed"
            )
        emit_progress(
            "complete",
            title=title,
            quality=quality,
            filename=name,
            folder=str(movie_path.parent),
            subtitle_count=0,
            percent=100,
            message="Movie is ready; no Sinhala or embedded English subtitle was found",
        )
        return (
            f"Movie downloaded: {movie_path}\n"
            "Sinhala subtitles: no matching files found\n"
            "Embedded English subtitles: none found"
        )


def download_movie_sync(title: str, year: int | None = None, quality: str = "720p") -> str:
    return asyncio.run(_download_movie(title, year, quality))


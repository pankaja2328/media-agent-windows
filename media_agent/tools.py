import gzip
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx
from ddgs import DDGS
from langchain_core.tools import tool

from .movie_downloader import download_movie_sync
from .subtitle_downloader import download_sinhala_subtitle_sync as fetch_sinhala_subtitle
from .subtitle_sync import select_and_sync_subtitles
from .progress import emit_progress


PROJECT_DIR = Path(__file__).resolve().parents[1]
LEGACY_DIR = PROJECT_DIR
TV_SCRIPT = LEGACY_DIR / "telegram_zip.py"
DOWNLOAD_ROOT = PROJECT_DIR / "downloads"
IMDB_CACHE_DIR = PROJECT_DIR / "cache"
IMDB_RATINGS_DB = IMDB_CACHE_DIR / "imdb_ratings.sqlite"
IMDB_RATINGS_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
IMDB_SUGGESTION_URL = "https://v3.sg.media-imdb.com/suggestion/x"
IMDB_CACHE_LOCK = threading.Lock()


def _search(query: str, max_results: int) -> list[dict]:
    errors = []
    # Startpage (used by auto) is refused on this network and DuckDuckGo
    # repeatedly times out. Google and Yandex are verified working fallbacks.
    for backend in ("google", "yandex", "brave", "yahoo"):
        try:
            results = DDGS(timeout=12).text(
                query,
                max_results=max_results,
                safesearch="moderate",
                backend=backend,
            )
            if results:
                return results
        except Exception as error:
            errors.append(f"{backend}: {error}")
    return [{"error": "Web search unavailable", "details": "; ".join(errors)}]


def _ensure_imdb_ratings_db() -> None:
    # Ratings do not need an 85 MB refresh on every day the app is opened.
    max_age = 7 * 24 * 60 * 60
    if IMDB_RATINGS_DB.exists() and time.time() - IMDB_RATINGS_DB.stat().st_mtime < max_age:
        return
    with IMDB_CACHE_LOCK:
        if IMDB_RATINGS_DB.exists() and time.time() - IMDB_RATINGS_DB.stat().st_mtime < max_age:
            return
        IMDB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        archive = IMDB_CACHE_DIR / "title.ratings.tsv.gz.part"
        database_part = IMDB_CACHE_DIR / "imdb_ratings.sqlite.part"
        print("Updating IMDb's public ratings dataset (one-time daily cache)...")
        try:
            with httpx.stream("GET", IMDB_RATINGS_URL, timeout=120, follow_redirects=True) as response:
                response.raise_for_status()
                with archive.open("wb") as output:
                    for chunk in response.iter_bytes():
                        output.write(chunk)

            database_part.unlink(missing_ok=True)
            connection = sqlite3.connect(database_part)
            try:
                connection.execute(
                    "CREATE TABLE ratings (imdb_id TEXT PRIMARY KEY, rating REAL, votes INTEGER)"
                )
                batch = []
                with gzip.open(archive, "rt", encoding="utf-8") as rows:
                    next(rows, None)
                    for row in rows:
                        imdb_id, rating, votes = row.rstrip("\n").split("\t")
                        batch.append((imdb_id, float(rating), int(votes)))
                        if len(batch) >= 10_000:
                            connection.executemany("INSERT INTO ratings VALUES (?, ?, ?)", batch)
                            batch.clear()
                    if batch:
                        connection.executemany("INSERT INTO ratings VALUES (?, ?, ?)", batch)
                connection.commit()
            finally:
                connection.close()
            database_part.replace(IMDB_RATINGS_DB)
        finally:
            archive.unlink(missing_ok=True)
            database_part.unlink(missing_ok=True)


def _rating_from_imdb_dataset(imdb_id: str) -> tuple[float | None, int | None]:
    _ensure_imdb_ratings_db()
    with sqlite3.connect(IMDB_RATINGS_DB) as connection:
        row = connection.execute(
            "SELECT rating, votes FROM ratings WHERE imdb_id = ?", (imdb_id,)
        ).fetchone()
    return (float(row[0]), int(row[1])) if row else (None, None)


def _normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


@lru_cache(maxsize=512)
def _imdb_id_from_suggestion(title: str, year: int | None = None) -> str | None:
    """Resolve a title quickly using IMDb's public suggestion service."""
    query = f"{title} {year}" if year else title
    try:
        response = httpx.get(
            f"{IMDB_SUGGESTION_URL}/{quote(query, safe='')}.json",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=8,
            follow_redirects=True,
        )
        response.raise_for_status()
        candidates = response.json().get("d", [])
    except (httpx.HTTPError, ValueError, TypeError):
        return None

    wanted = _normalized_title(title)
    matches = []
    for position, candidate in enumerate(candidates):
        imdb_id = candidate.get("id", "")
        candidate_title = candidate.get("l", "")
        if not re.fullmatch(r"tt\d+", imdb_id):
            continue
        if _normalized_title(candidate_title) != wanted:
            continue
        candidate_year = candidate.get("y")
        year_score = 1 if year is not None and candidate_year == year else 0
        # IMDb returns the most relevant/popular exact title first. Preserve
        # that ordering unless the caller supplied a matching year.
        matches.append((year_score, -position, imdb_id))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][2]


@tool
def web_search(query: str, max_results: int = 6) -> str:
    """Search the current web. Use for recent media facts, lists, and official sources."""
    max_results = max(1, min(max_results, 10))
    results = _search(query, max_results)
    return json.dumps(results, ensure_ascii=False)


@tool
def discover_similar_titles(
    seed_title: str,
    media_type: Literal["movie", "series"] = "movie",
    max_results: int = 8,
) -> str:
    """Find web candidates similar to a seed movie or TV series; verify ratings separately."""
    kind = "movies" if media_type == "movie" else "TV series"
    query = f"best {kind} like {seed_title} similar recommendations"
    results = _search(query, max(3, min(max_results, 10)))
    return json.dumps(results, ensure_ascii=False)


@lru_cache(maxsize=512)
def _imdb_from_web(title: str, year: int | None = None) -> dict:
    # Normal titles resolve in one small request, then ratings come from the
    # local official IMDb dataset. Slow search engines remain fallback-only.
    imdb_id = _imdb_id_from_suggestion(title, year)
    if imdb_id:
        try:
            rating, votes = _rating_from_imdb_dataset(imdb_id)
            if rating is not None:
                return {
                    "Title": title,
                    "Year": year,
                    "imdbRating": rating,
                    "imdbID": imdb_id,
                    "imdbVotes": votes,
                    "verifiedFromWeb": True,
                    "evidence": [
                        {
                            "title": "IMDb public ratings dataset",
                            "url": f"https://www.imdb.com/title/{imdb_id}/",
                            "snippet": f"IMDb rating {rating}/10 from {votes:,} votes",
                        }
                    ],
                }
        except (OSError, sqlite3.Error, httpx.HTTPError, ValueError):
            pass

    year_text = f" {year}" if year else ""
    results = _search(f'site:imdb.com/title "{title}"{year_text} IMDb rating', 8)
    rating = None
    evidence = []
    for result in results:
        href = result.get("href", "")
        body = result.get("body", "")
        heading = result.get("title", "")
        if "imdb.com/title" not in href.lower():
            continue
        evidence.append({"title": heading, "url": href, "snippet": body})
        id_match = re.search(r"/title/(tt\d+)", href)
        if id_match and not imdb_id:
            imdb_id = id_match.group(1)
        for pattern in (
            r"IMDb\s*(?:RATING|rating)?\s*[:\-]?\s*(\d(?:\.\d)?)(?:\s*/\s*10)?",
            r"\b(\d(?:\.\d)?)\s*/\s*10\b",
        ):
            match = re.search(pattern, f"{heading} {body}", re.I)
            if match:
                candidate = float(match.group(1))
                if 0 <= candidate <= 10:
                    rating = candidate
                    break
        if rating is not None:
            break
    votes = None
    if rating is None and imdb_id:
        try:
            rating, votes = _rating_from_imdb_dataset(imdb_id)
            if rating is not None:
                evidence.insert(
                    0,
                    {
                        "title": "IMDb public ratings dataset",
                        "url": IMDB_RATINGS_URL,
                        "snippet": f"IMDb rating {rating}/10 from {votes:,} votes",
                    },
                )
        except (OSError, sqlite3.Error, httpx.HTTPError, ValueError):
            rating = None
    # The official dataset is much faster and more reliable. Only fetch the
    # public title page when a newly listed title is not present in the dataset.
    if rating is None and imdb_id:
        try:
            page = httpx.get(
                f"https://www.imdb.com/title/{imdb_id}/",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/131 Safari/537.36",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                follow_redirects=True,
                timeout=20,
            )
            page.raise_for_status()
            match = re.search(
                r'"ratingValue"\s*:\s*"?(\d(?:\.\d)?)"?', page.text, re.I
            )
            if match:
                rating = float(match.group(1))
                evidence.insert(
                    0,
                    {
                        "title": f"{title} - IMDb structured page metadata",
                        "url": f"https://www.imdb.com/title/{imdb_id}/",
                        "snippet": f"IMDb rating value found in page metadata: {rating}/10",
                    },
                )
        except (httpx.HTTPError, ValueError):
            pass
    return {
        "Title": title,
        "Year": year,
        "imdbRating": rating if rating is not None else "N/A",
        "imdbID": imdb_id,
        "imdbVotes": votes,
        "verifiedFromWeb": rating is not None,
        "evidence": evidence[:3],
    }


@tool
def imdb_lookup(title: str, year: int | None = None) -> str:
    """Find current IMDb-rating evidence for one title using web search results."""
    return json.dumps(_imdb_from_web(title, year), ensure_ascii=False)


@tool
def filter_imdb_titles(
    titles: list[str],
    minimum_rating: float = 6.0,
    media_type: Literal["movie", "series", "any"] = "any",
) -> str:
    """Web-check candidates and retain only titles with verifiable IMDb ratings above a threshold."""
    accepted = []
    candidates = titles[:15]
    # Web and IMDb requests are independent. Running a bounded group in
    # parallel cuts a 10-title recommendation from minutes to seconds without
    # overwhelming the search providers.
    with ThreadPoolExecutor(max_workers=min(10, max(1, len(candidates)))) as executor:
        verified = list(executor.map(_imdb_from_web, candidates))
    for data in verified:
        if not data["verifiedFromWeb"]:
            continue
        rating = float(data["imdbRating"])
        if rating < minimum_rating:
            continue
        accepted.append(
            {
                "Title": data["Title"],
                "TypeRequested": media_type,
                "imdbRating": rating,
                "imdbID": data["imdbID"],
                "evidence": data["evidence"],
            }
        )
    accepted.sort(key=lambda item: item["imdbRating"], reverse=True)
    return json.dumps(accepted, ensure_ascii=False)


@tool
def legal_watch_options(title: str, country: str = "Sri Lanka") -> str:
    """Search for current official streaming, rental, or purchase options in a country."""
    query = f"{title} official streaming rent buy watch {country}"
    return json.dumps(_search(query, 8), ensure_ascii=False)


def _tv_eta_seconds(value: str) -> int | None:
    try:
        parts = [int(part) for part in value.split(":")]
    except ValueError:
        return None
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def _publish_tv_status(line: str, title: str, season: int, quality: str) -> None:
    line = line.strip()
    if not line:
        return
    display_title = f"{title} S{season:02d}"
    progress = re.search(
        r"\]\s*([\d.]+)%\s+([\d,.]+)/([\d,.]+)\s+MB\s+"
        r"([\d,.]+)\s+MB/s\s+ETA\s+([\d:]+)",
        line,
        re.I,
    )
    if progress:
        current_mb = float(progress.group(2).replace(",", ""))
        total_mb = float(progress.group(3).replace(",", ""))
        speed_mb = float(progress.group(4).replace(",", ""))
        emit_progress(
            "downloading",
            title=display_title,
            quality=quality,
            filename="Season episode batch",
            percent=float(progress.group(1)),
            current=int(current_mb * 1024 * 1024),
            total=int(total_mb * 1024 * 1024),
            speed=speed_mb * 1024 * 1024,
            eta=_tv_eta_seconds(progress.group(5)),
            message="Downloading season episodes concurrently",
        )
        return
    if "Selecting:" in line:
        filename = line.split("Selecting:", 1)[1].strip()
        emit_progress(
            "selected",
            title=display_title,
            quality=quality,
            filename=filename,
            message="Selecting matching season episodes",
        )
    elif "Status:" in line:
        message = line[line.find("Status:") + 7 :].strip()
        emit_progress(
            "searching",
            title=display_title,
            quality=quality,
            message=message,
        )
    elif "Opening next results page:" in line:
        emit_progress(
            "searching",
            title=display_title,
            quality=quality,
            message="Scanning the next Telegram results page",
        )
    elif "Following:" in line:
        emit_progress(
            "selected",
            title=display_title,
            quality=quality,
            message="Requesting selected episode files from Telegram",
        )
    elif "Detected episodes:" in line:
        episodes = line.split("Detected episodes:", 1)[1].strip()
        emit_progress(
            "selected",
            title=display_title,
            quality=quality,
            filename=episodes,
            message=f"Episodes detected: {episodes}",
        )
    elif "Downloading " in line and "file(s) concurrently" in line:
        emit_progress(
            "downloading",
            title=display_title,
            quality=quality,
            percent=0,
            message="Starting concurrent episode downloads",
        )
    elif "Adding completed batch to ZIP" in line:
        emit_progress(
            "downloading",
            title=display_title,
            quality=quality,
            percent=100,
            message="Episodes downloaded — building the season ZIP",
        )
    elif "ZIP ready at" in line:
        emit_progress(
            "subtitles",
            title=display_title,
            quality=quality,
            percent=100,
            message="Season ZIP ready — searching and syncing subtitles",
        )


@tool
def download_tv_series(
    title: str,
    season: int,
    quality: Literal["720p", "1080p", "2160p"] = "720p",
    authorized: bool = False,
) -> str:
    """Download and ZIP one TV season at 720p, 1080p, or 2160p. Defaults to 720p and requires authorization."""
    if not authorized:
        return "Blocked: explicit confirmation of lawful download authorization is required."
    if not TV_SCRIPT.exists():
        return f"TV downloader not found at {TV_SCRIPT}"
    before = set(DOWNLOAD_ROOT.rglob("*.zip")) if DOWNLOAD_ROOT.exists() else set()
    request = f"{title} S{season:02d}\ny\n"
    environment = os.environ.copy()
    environment["TELEGRAM_DOWNLOAD_ROOT"] = str(DOWNLOAD_ROOT)
    environment["TELEGRAM_QUALITY"] = quality
    environment["PYTHONUNBUFFERED"] = "1"
    display_title = f"{title} S{season:02d}"
    emit_progress(
        "searching",
        title=display_title,
        quality=quality,
        percent=0,
        message="Searching every Telegram results page for season episodes",
    )
    process = subprocess.Popen(
        [sys.executable, "-u", str(TV_SCRIPT)],
        cwd=LEGACY_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=environment,
    )
    process.stdin.write(request)
    process.stdin.flush()
    process.stdin.close()
    output_line = ""
    while True:
        character = process.stdout.read(1)
        if character == "":
            break
        if character in {"\r", "\n"}:
            if output_line.strip():
                print(output_line.strip())
                _publish_tv_status(output_line, title, season, quality)
            output_line = ""
        else:
            output_line += character
    if output_line.strip():
        print(output_line.strip())
        _publish_tv_status(output_line, title, season, quality)
    returncode = process.wait()
    after = set(DOWNLOAD_ROOT.rglob("*.zip")) if DOWNLOAD_ROOT.exists() else set()
    created = sorted(after - before, key=lambda path: path.stat().st_mtime, reverse=True)
    if returncode == 0 and created:
        emit_progress(
            "subtitles",
            title=display_title,
            quality=quality,
            percent=100,
            message="Searching Sinhala subtitles for every episode",
        )
        subtitles = fetch_sinhala_subtitle(
            title,
            created[0].parent,
            season=season,
            preferred_quality=quality,
        )
        sync_results = select_and_sync_subtitles(created[0].parent)
        result = f"TV season ZIP created: {created[0]}"
        if subtitles:
            result += "\nSubtitle candidates:\n" + "\n".join(
                f"  - {path}" for path in subtitles
            )
        else:
            result += "\nSinhala subtitles: no matching files found"
        synchronized = [item for item in sync_results if item.get("subtitle")]
        failed = [item for item in sync_results if not item.get("subtitle")]
        if synchronized:
            result += "\nSynchronized episode subtitles:\n" + "\n".join(
                f"  - {item['subtitle']}" for item in synchronized
            )
        if failed:
            result += "\nEpisodes without a synchronized subtitle:\n" + "\n".join(
                f"  - {Path(item['video']).name}: {item['error']}" for item in failed
            )
        emit_progress(
            "complete",
            title=display_title,
            quality=quality,
            percent=100,
            folder=str(created[0].parent),
            subtitle_count=len(synchronized),
            message=f"Season ready — synchronized {len(synchronized)} episode subtitle(s)",
        )
        return result
    emit_progress(
        "error",
        title=display_title,
        quality=quality,
        message=f"TV download stopped with exit code {returncode}",
    )
    return f"TV download did not complete successfully (exit code {returncode})."


@tool
def download_movie(
    title: str,
    year: int | None = None,
    quality: Literal["720p", "1080p", "2160p"] = "720p",
    authorized: bool = False,
) -> str:
    """Download one movie at 720p, 1080p, or 2160p. Defaults to 720p and requires authorization."""
    if not authorized:
        return "Blocked: explicit confirmation of lawful download authorization is required."
    return download_movie_sync(title=title, year=year, quality=quality)


@tool
def download_sinhala_subtitle_file(
    title: str,
    year: int | None = None,
    season: int | None = None,
    destination_folder: str = "",
    preferred_quality: Literal["720p", "1080p", "2160p"] = "720p",
) -> str:
    """Search @Sinhala_Subtitles_Bot and save one Sinhala subtitle in a local folder."""
    destination = Path(destination_folder) if destination_folder else PROJECT_DIR / "downloads"
    results = fetch_sinhala_subtitle(
        title,
        destination,
        year=year,
        season=season,
        preferred_quality=preferred_quality,
    )
    if not results:
        return "No matching Sinhala subtitles found."
    return "Sinhala subtitles saved:\n" + "\n".join(f"  - {path}" for path in results)


@tool
def select_and_sync_sinhala_subtitle(media_folder: str) -> str:
    """Choose the best local subtitle, synchronize it to video audio, and rename it to the video name."""
    try:
        results = select_and_sync_subtitles(media_folder)
    except (FileNotFoundError, OSError, RuntimeError) as error:
        return f"Subtitle synchronization failed: {error}"
    if not results:
        return "No video files were found in that folder."
    lines = []
    for item in results:
        if item.get("subtitle"):
            lines.append(
                f"Synchronized: {item['subtitle']} (source: {item['source_subtitle']})"
            )
        else:
            lines.append(f"Not synchronized: {item['video']} - {item['error']}")
    return "\n".join(lines)


ALL_TOOLS = [
    web_search,
    discover_similar_titles,
    imdb_lookup,
    filter_imdb_titles,
    legal_watch_options,
    download_tv_series,
    download_movie,
    download_sinhala_subtitle_file,
    select_and_sync_sinhala_subtitle,
]


import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}


def _ffmpeg_path() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    packages = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    matches = sorted(packages.glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"), reverse=True)
    return str(matches[0]) if matches else None


def _ffprobe_path() -> str | None:
    ffmpeg = _ffmpeg_path()
    if ffmpeg:
        sibling = Path(ffmpeg).with_name("ffprobe.exe")
        if sibling.exists():
            return str(sibling)
    return shutil.which("ffprobe")


def extract_embedded_english_subtitle(video: str | Path) -> Path | None:
    """Extract a text-based embedded English subtitle as a same-name SRT."""
    video = Path(video).expanduser().resolve()
    ffmpeg = _ffmpeg_path()
    ffprobe = _ffprobe_path()
    if not video.is_file() or not ffmpeg or not ffprobe:
        return None
    try:
        probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "s",
                "-show_entries",
                "stream=index,codec_name:stream_tags=language,title",
                "-of",
                "json",
                str(video),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=True,
        )
        streams = json.loads(probe.stdout or "{}").get("streams", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

    text_codecs = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
    selected = None
    for stream in streams:
        tags = stream.get("tags") or {}
        language = str(tags.get("language", "")).casefold()
        title = str(tags.get("title", "")).casefold()
        codec = str(stream.get("codec_name", "")).casefold()
        is_english = language in {"en", "eng", "english"} or "english" in title
        if is_english and codec in text_codecs:
            selected = stream.get("index")
            break
    if selected is None:
        return None

    # Keep English as a backup. The exact-name .srt is reserved for the final
    # Sinhala subtitle created from this track.
    output = video.with_name(f"{video.stem}.en.srt")
    temporary = output.with_name(f"{output.stem}.english.part.srt")
    temporary.unlink(missing_ok=True)
    try:
        completed = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(video),
                "-map",
                f"0:{selected}",
                "-c:s",
                "srt",
                "-y",
                str(temporary),
            ],
            capture_output=True,
            timeout=5 * 60,
        )
        if completed.returncode != 0 or not temporary.exists() or temporary.stat().st_size == 0:
            temporary.unlink(missing_ok=True)
            return None
        temporary.replace(output)
        return output
    except (OSError, subprocess.SubprocessError):
        temporary.unlink(missing_ok=True)
        return None


def _tokens(value: str) -> set[str]:
    value = value.casefold().replace("web-dl", "webdl").replace("web dl", "webdl")
    return set(re.findall(r"[a-z0-9]+", value))


def _release_score(video: Path, subtitle: Path) -> int:
    video_tokens = _tokens(video.stem)
    subtitle_tokens = _tokens(subtitle.stem)
    score = len(video_tokens & subtitle_tokens)
    groups = [
        {"480p", "720p", "1080p", "2160p", "4k"},
        {"bluray", "brrip", "webrip", "webdl", "hdtv", "dvdrip"},
        {"x264", "h264", "avc", "x265", "h265", "hevc"},
    ]
    for group in groups:
        video_values = video_tokens & group
        subtitle_values = subtitle_tokens & group
        if video_values and subtitle_values:
            score += 8 if video_values & subtitle_values else -20
    video_years = {item for item in video_tokens if re.fullmatch(r"(?:19|20)\d{2}", item)}
    subtitle_years = {
        item for item in subtitle_tokens if re.fullmatch(r"(?:19|20)\d{2}", item)
    }
    if video_years and subtitle_years:
        score += 12 if video_years & subtitle_years else -40
    return score


def _safe_extract_subtitles(archive: Path, destination: Path) -> list[Path]:
    extracted = []
    try:
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                name = Path(member.filename).name
                if not name or Path(name).suffix.casefold() not in SUBTITLE_EXTENSIONS:
                    continue
                target = destination / name
                counter = 1
                while target.exists():
                    target = destination / f"{Path(name).stem} ({counter}){Path(name).suffix}"
                    counter += 1
                with source.open(member) as input_file, target.open("wb") as output_file:
                    shutil.copyfileobj(input_file, output_file)
                extracted.append(target)
    except (OSError, zipfile.BadZipFile):
        pass
    return extracted


def _sync_candidate(video: Path, candidate: Path, output: Path) -> dict:
    from ffsubsync.ffsubsync import make_parser, run

    arguments = [
        str(video),
        "-i",
        str(candidate),
        "-o",
        str(output),
        "--skip-sync-on-low-quality",
        "--quality-max-offset-seconds",
        "30",
        "--max-framerate-deviation",
        "0.10",
    ]
    ffmpeg = _ffmpeg_path()
    if ffmpeg:
        arguments.extend(["--ffmpeg-path", ffmpeg])
    parser = make_parser()
    return run(parser.parse_args(arguments))


def select_and_sync_subtitles(media_folder: str | Path) -> list[dict]:
    """Select and audio-sync one subtitle for every video in a folder."""
    folder = Path(media_folder).expanduser().resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"Media folder does not exist: {folder}")
    videos = sorted(
        path for path in folder.iterdir() if path.is_file() and path.suffix.casefold() in VIDEO_EXTENSIONS
    )
    if not videos:
        return []

    loose = [
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.casefold() in SUBTITLE_EXTENSIONS
    ]
    archives = [
        path for path in folder.iterdir() if path.is_file() and path.suffix.casefold() == ".zip"
    ]
    results = []
    with tempfile.TemporaryDirectory(prefix="subtitle_candidates_", dir=folder) as temp_name:
        temp = Path(temp_name)
        extracted = []
        for archive in archives:
            extracted.extend(_safe_extract_subtitles(archive, temp))
        candidates = loose + extracted
        for video in videos:
            final_path = video.with_suffix(".srt")
            usable = [path for path in candidates if path.resolve() != final_path.resolve()]
            usable.sort(key=lambda path: _release_score(video, path), reverse=True)
            selected = None
            last_error = None
            for index, candidate in enumerate(usable, start=1):
                temporary_output = temp / f"synced_{index}.srt"
                temporary_output.unlink(missing_ok=True)
                try:
                    sync_result = _sync_candidate(video, candidate, temporary_output)
                except Exception as error:
                    last_error = str(error)
                    continue
                if (
                    sync_result.get("retval") == 0
                    and sync_result.get("sync_was_successful") is True
                    and temporary_output.exists()
                    and temporary_output.stat().st_size > 0
                ):
                    temporary_output.replace(final_path)
                    selected = {
                        "video": str(video),
                        "source_subtitle": str(candidate),
                        "subtitle": str(final_path),
                        "offset_seconds": sync_result.get("offset_seconds"),
                        "framerate_scale_factor": sync_result.get(
                            "framerate_scale_factor"
                        ),
                    }
                    break
            if selected:
                results.append(selected)
            else:
                results.append(
                    {
                        "video": str(video),
                        "subtitle": None,
                        "error": last_error or "No subtitle passed the audio synchronization check.",
                    }
                )
    return results

"""Fail when files selected for Git contain likely credentials or private runtime data."""

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FORBIDDEN_NAMES = {"credentials.bin", "telegram_config.bin", "telegram_config.json"}
FORBIDDEN_SUFFIXES = {
    ".session",
    ".session-journal",
    ".mkv",
    ".mp4",
    ".srt",
    ".ass",
    ".ssa",
    ".vtt",
    ".sub",
}
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Google API key": re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    "GitHub token": re.compile(r"gh[opusr]_[0-9A-Za-z]{30,}"),
    "generic exposed key": re.compile(r"\bAQ\.[0-9A-Za-z_-]{20,}"),
    "hardcoded Telegram API hash": re.compile(
        r"telegram_api_hash\s*=\s*['\"][0-9a-fA-F]{32}['\"]", re.I
    ),
    "hardcoded Telegram API ID": re.compile(r"telegram_api_id\s*=\s*\d{5,}", re.I),
}


def tracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    names = [line for line in completed.stdout.splitlines() if line.strip()]
    if not names:
        completed = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        names = [line for line in completed.stdout.splitlines() if line.strip()]
    return [ROOT / line for line in names]


def main() -> int:
    findings = []
    for path in tracked_files():
        relative = path.relative_to(ROOT)
        lower_parts = {part.casefold() for part in relative.parts}
        if (
            path.name.casefold() in FORBIDDEN_NAMES
            or path.suffix.casefold() in FORBIDDEN_SUFFIXES
            or "downloads" in lower_parts
            or "cache" in lower_parts
            or ".venv" in lower_parts
        ):
            findings.append(f"private runtime file staged: {relative}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.name == Path(__file__).name:
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{label}: {relative}")
    if findings:
        print("SECURITY CHECK FAILED")
        for finding in findings:
            print(f"  - {finding}")
        return 1
    print(f"Security check passed for {len(tracked_files())} staged files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

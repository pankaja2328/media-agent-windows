import ctypes
import getpass
import json
import os
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_DIR / "credentials.bin"
REQUIRED_FIELDS = {
    "gemini_api_key",
    "telegram_api_id",
    "telegram_api_hash",
    "media_bot_username",
    "subtitle_bot_username",
}


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _protect(raw: bytes) -> bytes:
    buffer = ctypes.create_string_buffer(raw)
    source = DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "Media Agent credentials", None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def _unprotect(encrypted: bytes) -> bytes:
    buffer = ctypes.create_string_buffer(encrypted)
    source = DataBlob(len(encrypted), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def _bot_username(prompt: str) -> str:
    value = input(prompt).strip()
    if not value:
        raise SystemExit("Bot username is required.")
    return value if value.startswith("@") else f"@{value}"


def configure_credentials() -> dict:
    print("\nAll secrets are encrypted with Windows DPAPI and stay only on this PC.")
    print("Nothing entered here is written to Git or uploaded to GitHub.\n")
    api_id_text = input("Telegram API ID: ").strip()
    if not api_id_text.isdigit():
        raise SystemExit("Telegram API ID must contain digits only.")
    data = {
        "telegram_api_id": int(api_id_text),
        "telegram_api_hash": getpass.getpass("Telegram API hash: ").strip(),
        "gemini_api_key": getpass.getpass("Gemini API key: ").strip(),
        "media_bot_username": _bot_username("Media search bot username: "),
        "subtitle_bot_username": _bot_username("Subtitle bot username: "),
    }
    if not data["telegram_api_hash"] or not data["gemini_api_key"]:
        raise SystemExit("Telegram API hash and Gemini API key are required.")
    CONFIG_PATH.write_bytes(_protect(json.dumps(data).encode("utf-8")))
    load_credentials.cache_clear()
    return data


@lru_cache(maxsize=1)
def load_credentials() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError("Credentials are not configured. Double-click setup.bat first.")
    try:
        data = json.loads(_unprotect(CONFIG_PATH.read_bytes()).decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("Encrypted credentials could not be read. Run setup.bat again.") from error
    missing = REQUIRED_FIELDS - data.keys()
    if missing:
        raise RuntimeError("Credential setup is incomplete. Run setup.bat again.")
    os.environ["GOOGLE_API_KEY"] = str(data["gemini_api_key"])
    return data.copy()

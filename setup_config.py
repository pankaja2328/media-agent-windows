import asyncio
import getpass
from pathlib import Path

from telethon import TelegramClient

from media_agent.credentials import configure_credentials


ROOT = Path(__file__).resolve().parent
SESSION_PATH = ROOT / "telegram_zip"


async def configure_telegram(data: dict) -> None:
    print("\nTelegram sign-in (the session file remains local and is ignored by Git):")
    phone = input("Phone number with country code (example +947xxxxxxxx): ").strip()
    if not phone:
        raise SystemExit("A Telegram phone number is required for the first sign-in.")
    client = TelegramClient(
        str(SESSION_PATH), int(data["telegram_api_id"]), data["telegram_api_hash"]
    )
    try:
        await client.start(
            phone=phone,
            code_callback=lambda: input("Telegram login code: ").strip(),
            password=lambda: getpass.getpass("Telegram 2-step password (if enabled): "),
        )
        me = await client.get_me()
        print(f"Telegram session ready for {getattr(me, 'first_name', 'account')}.")
    finally:
        await client.disconnect()


def main() -> None:
    data = configure_credentials()
    asyncio.run(configure_telegram(data))
    print("\nSetup complete. Double-click run.bat to start Media Agent.")


if __name__ == "__main__":
    main()

# Media Agent for Windows

A responsive local browser assistant for movie and TV discovery, IMDb verification,
authorized Telegram media workflows, Sinhala subtitle matching, embedded English
subtitle extraction, and Gemini-assisted English-to-Sinhala subtitle translation.

## Quick start after cloning

Requirements: Windows 10/11 and Python 3.11 or newer.

1. Double-click **`setup.bat`**.
2. Enter your Telegram API ID and API hash, Gemini API key, media bot username,
   subtitle bot username, and Telegram sign-in code when requested.
3. After setup completes, double-click **`run.bat`**.

Future launches require only `run.bat`. The browser interface opens automatically.

## Private configuration

`setup.bat` stores all configuration in `credentials.bin`, encrypted using Windows
DPAPI. The encrypted file works only for the Windows account that created it. The
Telegram authorization is stored in a local `.session` file. Both are excluded by
`.gitignore`, along with downloads, subtitles, caches, and virtual environments.

Never send `credentials.bin` or a Telegram `.session` file to another person. If a
credential may have been exposed, revoke or rotate it at its provider.

## What setup requests

- Gemini API key from Google AI Studio
- Telegram API ID and API hash from Telegram's developer portal
- A Telegram media-search bot username
- A Telegram subtitle bot username
- Your phone number and Telegram login code for the local session

The phone number and login code are used only by Telethon during sign-in and are not
written to the encrypted configuration file.

## Features

- Responsive animated browser chat for desktop and mobile-sized displays
- Ten-item recommendation cards with verified IMDb ratings
- Requested 720p, 1080p, or 2160p release selection (720p default)
- Live download percentage, speed, size, ETA, and processing stages
- Concurrent Telegram transfers with 512 KB chunks
- Safe ZIP subtitle extraction with archive limits and path-traversal protection
- Sinhala subtitle selection and audio synchronization
- Embedded English subtitle extraction when Sinhala subtitles are unavailable
- Gemini batch translation from English SRT to Sinhala while preserving timestamps

## Running from a terminal

- `run.bat` — browser interface
- `run_cli.bat` — terminal chat
- `setup.bat` — first setup or credential replacement

## Legal use

Use download features only for content you own, content you are licensed to access,
or public-domain material. You are responsible for complying with applicable laws,
platform terms, and content licenses.

## Troubleshooting

- If setup fails, run `setup.bat` again.
- If credentials change, run `setup.bat` again to replace the encrypted configuration.
- Keep the command window open while the browser interface is running.
- FFmpeg and FFprobe are needed for subtitle synchronization and embedded-track
  extraction. Install FFmpeg and ensure both commands are available on `PATH`.

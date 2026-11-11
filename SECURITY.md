# Security policy

## Sensitive local files

The following files must never be committed, uploaded, shared, or attached to an
issue:

- `credentials.bin`
- `*.session` and `*.session-journal`
- files inside `downloads/` or `cache/`
- subtitle, video, ZIP, log, or partial-download files
- `.venv/`

The repository `.gitignore` excludes these paths. Before pushing changes, inspect
`git status` and the staged diff.

## Credential storage

API credentials and bot configuration are stored in one DPAPI-encrypted local file.
The Telegram session is maintained by Telethon in a separate ignored session file.
No credential is expected in source code, batch files, documentation, environment
templates, or command-line arguments.

## Reporting a vulnerability

Report vulnerabilities privately to the repository owner. Do not include real API
keys, session data, phone numbers, downloaded media, or other personal information.

# CLAUDE.md

Config via env vars: `AYON_SERVER_URL`, `AYON_API_KEY`.

Integration tests need `AYON_SERVER_URL`/`AYON_API_KEY` set or a `.env` in `tests/`
(pytest mark `integration`).

Build the addon package: `python -X dev ./create_package.py`.

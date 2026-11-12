# Repository Guidelines

## Project Structure & Module Organization

- `src/smartdaily_web/main.py` defines the FastAPI app and background polling workflow.
- `smartdaily_client.py` handles SmartDaily requests and response normalization; `push_service.py` manages local records and Web Push delivery, while `database.py` defines the SQLite schema and migrations.
- `src/smartdaily_web/static/` contains the HTML, service worker, manifest, and icons.
- `tests/test_app.py` contains the pytest suite. Runtime files such as the SQLite database, VAPID keys, and downloaded attachments belong under `data/`, not in source control. Legacy message and device JSON files are imported on the first database migration and retained as backups.

## Build, Test, and Development Commands

- `uv sync --locked` installs the locked project and development dependencies.
- `DEVICE_ID="..." uv run smartdaily-web` starts the local service.
- `uv run pytest -v` runs the unit tests.
- `uv run ruff check .` checks Python lint; `uv run ruff format --check .` checks formatting.
- `docker compose up -d` starts the configured container service.

## Coding Style & Naming Conventions

Use Python 3.10+, four-space indentation, and the Ruff settings in `pyproject.toml` (100-character line length). Name Python functions and variables in `snake_case`; name tests `test_<behavior>`. Keep frontend JavaScript consistent with the two-space indentation and `camelCase` already used in `static/index.html`.

## Testing Guidelines

Use pytest with `tmp_path` for filesystem state and `monkeypatch` for external clients and push delivery. Keep tests deterministic and independent of a real SmartDaily account or network access. Cover pagination, per-device delivery/retry behavior, and SQLite reads/writes when changing those paths. Add a migration test whenever the schema or legacy import changes.

For behavior changes, use red-green-refactor: add one focused test at a public behavior boundary, run it and confirm the expected failure, then make the smallest implementation that passes. Run the focused test and full suite before refactoring; rerun affected checks afterward. Documentation-only and formatting-only changes use their relevant checks, such as Ruff format, without adding behavior tests.

## Commit & Pull Request Guidelines

Recent commits use short Conventional Commit prefixes such as `feat:`, `fix:`, `test:`, and `docs:`. Use the matching prefix and describe the behavior changed. Pull requests should summarize user-visible changes, list test and lint commands run, link related issues when available, and include screenshots for UI changes.

## Security & Architecture Notes

Only the background poller should call SmartDaily APIs; web routes and the frontend read locally stored data. Define schema changes as versioned migrations in `src/smartdaily_web/database.py`; startup applies pending migrations before starting the poller. Keep `DEVICE_ID`, VAPID secrets, and other credentials in environment configuration, never in commits. Do not commit runtime data or downloaded resident documents.

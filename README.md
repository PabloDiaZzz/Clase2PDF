# Clase2PDF Blueprint

## System Overview
Self-updating Windows CLI tool (`procesar_clase.bat` + `pipeline.py`) that cleans classroom audio with adaptive `ffmpeg` (`ebur128`, anti-pumping `alimiter`, `afftdn`, `dynaudnorm`), extracts technical vocabulary via Flash models, transcribes/reviews via Gemini with multi-API-key rotation and topic-based large-PDF slicing (`pypdf`), and exports clean study notes or transcripts to PDF (and optional Markdown) via headless Edge/Chrome.

## Local Interface & Invariants
- **Bootstrapper (`procesar_clase.bat`)**:
  - Env overrides: `CLASE2PDF_REPO_URL` (default `https://raw.githubusercontent.com/PabloDiaZzz/Clase2PDF/main`), `CLASE2PDF_NO_UPDATE=1`, `CLASE2PDF_BAT_REEXEC=1`, `LOCALAPPDATA`.
  - Downloads and SHA-256-verifies `manifest.json`, `pipeline.py`, `procesar_clase.bat`, and (non-blocking into the `.bat` folder) `INSTRUCCIONES.md` before execution; falls back to cached `%LOCALAPPDATA%\Clase2PDF\pipeline.py` when offline.
  - Installs missing `ffmpeg` / `Python 3.12` via `winget` (checks `where winget`) and provisions `%LOCALAPPDATA%\Clase2PDF\venv`.
- **CLI Entrypoint (`pipeline.py:main`, `pipeline.py:build_parser`)**:
  - Flags: `carpeta` (positional, default cwd), `--version` (`1.9.0`), `--mode {transcripcion,apuntes}`, `--keep-md`, `--apis`, `--add-api`, `--reset-key`, `--dry-run`, `--json`, `-q/--quiet`, `--no-clear`, `--debug`.
  - Exit codes: `EXIT_OK=0`, `EXIT_ERROR=1`, `EXIT_USAGE=2`, `EXIT_NOT_FOUND=3`, `EXIT_AUTH=4`, `EXIT_TRANSIENT=75`, `EXIT_CONFIG=78`, `EXIT_CANCELLED=130`.
- **Key Components (`pipeline.py`)**:
  - `ClientPool`, `get_or_prompt_api_keys`, `add_api_key_flow`, `manage_api_keys_menu`, `test_api_key` — Windows DPAPI multi-key storage (`api_key.dpapi`, `api_key_<N>.dpapi`) and sticky same-model rotation on `429`/auth vs immediate model fallback on `503` (`server_busy`).
  - `extract_vocabulary` (`VOCAB_MODELS_CHAIN`), `select_and_slice_docs`, `plan_large_pdf_topics` (`TRIAGE_MODELS_CHAIN`), `inspect_pdf_pages`, `slice_pdf_pages` — Chunked vocabulary and verbatim `pypdf` page slicing without summarization.
  - `clean_and_split_audios`, `transcribe_chunks`, `synthesize_notes`, `export_pdf`, `cleanup_intermediate` — Atomic `.tmp` writes, audio fingerprint caching, and full `.clase_pipeline/` cleanup on completion.
- **Invariants**:
  - Uploaded files (`_upload_local_file`) MUST stream as binary (`open("rb")`) with explicit `mime_type` to prevent `UnicodeEncodeError` in HTTP headers on accented filenames.
  - Uploaded Gemini remote files MUST be deleted using the same `ClientPool` client that uploaded them.
  - Runtime state (`venv`, `api_key*.dpapi`, `pipeline.py`, `last_error.log`) MUST live in `%LOCALAPPDATA%\Clase2PDF`, never inside synced cloud folders.
  - API keys MUST NEVER be printed or logged in plaintext.

## Subdirectories & System Boundaries
- [tools](./tools/README.md) - Release manifest generator (`build_manifest.py`) and local runtime sync.

## Global Invariants
- `manifest.json` SHA-256 digests (`pipeline_sha256`, `bat_sha256`, `req_hash`) MUST stay synchronized with `pipeline.py`, `procesar_clase.bat`, and `tools/build_manifest.py:REQUIREMENTS` on every release.
- Interactive terminal output in normal mode avoids raw stack traces (`last_error.log` captures tracebacks) and pauses on double-click or folder drag-and-drop.

## Global Side Effects
- Network requests to GitHub Raw (`manifest.json`, `pipeline.py`, `procesar_clase.bat`) and Google Gemini API (`v1beta`).
- Local filesystem writes in `%LOCALAPPDATA%\Clase2PDF\` and transient `.clase_pipeline\` in the target class folder, plus output PDFs (`APUNTES_*.pdf`, `TRANSCRIPCION_*.pdf`) and optional `.md` files.
- Headless Edge/Chrome process invocation for HTML-to-PDF rendering (with pure-Python `fpdf2` fallback when neither browser is installed).

## System Verification
- Run: `python tests/verify_all.py && python tests/verify_rich_ui.py && python tests/verify_cli_contract.py && python tests/verify_v13.py && python tests/verify_v19.py`

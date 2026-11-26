# tools

## Overview
Build and release tooling for `Clase2PDF`. Normalizes line endings, computes SHA-256 digests for `pipeline.py`, `procesar_clase.bat`, and Python package requirements, writes the root `manifest.json`, and synchronizes local runtime/OneDrive copies.

## Interface
- `build_manifest.py:REQUIREMENTS` — Ordered list of required pip packages (`["google-genai", "rich", "questionary", "pypdf"]`).
- `build_manifest.py:_sha256_hex(path: Path) -> str` — Computes lowercase SHA-256 digest of a target file.
- `build_manifest.py:_extract_version(pipeline_path: Path) -> str` — Extracts `APP_VERSION` from `../pipeline.py`.
- `build_manifest.py:main()` — Normalizes line endings (`pipeline.py` and `INSTRUCCIONES.md` to LF, `procesar_clase.bat` to CRLF), writes `../manifest.json`, and syncs `procesar_clase.bat`, `INSTRUCCIONES.md`, and `pipeline.py` to local runtime/OneDrive paths.

## Invariants
- `pipeline.py` and `INSTRUCCIONES.md` MUST use LF (`\n`) and `procesar_clase.bat` MUST use CRLF (`\r\n`) before hashing so local SHA-256 values in `manifest.json` match `.gitattributes` and GitHub Raw downloads byte-for-byte.
- `manifest.json` MUST contain `version`, `pipeline_sha256`, `bat_sha256`, `req_hash`, `requirements`, and (when `INSTRUCCIONES.md` exists) `instructions_sha256`.

## Side Effects
- Rewrites `../pipeline.py`, `../procesar_clase.bat`, and `../manifest.json` in place.
- Copies `procesar_clase.bat` to `C:\Users\pablo\OneDrive\Desktop\Clase2PDF\procesar_clase.bat` when that directory exists.
- Copies `pipeline.py` and writes `.pipe_hash`, `.req_pkgs`, `.req_hash_want`, and `.req_hash_done` inside `%LOCALAPPDATA%\Clase2PDF\`.

## Verification
- Run: `python tools/build_manifest.py && python tests/verify_rich_ui.py`

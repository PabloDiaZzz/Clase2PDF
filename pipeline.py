"""Pipeline automatico: Audio(s) + Docs de contexto -> Apuntes .pdf con interfaz Rich."""

from __future__ import annotations

import argparse
import atexit
import ctypes
from ctypes import wintypes
import hashlib
import html
import json
import math
import mimetypes
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Prompt
from rich.table import Table
from rich.theme import Theme
from rich.tree import Tree

THEME = Theme(
    {
        "accent": "bold cyan",
        "stage": "bold bright_blue",
        "ok": "bold green",
        "warn": "bold yellow",
        "err": "bold red",
        "muted": "dim",
        "model": "magenta",
    }
)
console = Console(theme=THEME)

APP_VERSION = "1.9.2"


def _load_genai():
    from google import genai
    from google.genai import types
    return genai, types
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_AUTH = 4
EXIT_TRANSIENT = 75
EXIT_CONFIG = 78
EXIT_CANCELLED = 130

AUDIO_EXTS = {".mp3", ".mp4", ".m4a", ".aac", ".mka", ".wav", ".ogg", ".opus", ".flac", ".webm"}
DOC_EXTS = {".pdf", ".docx"}
OUTPUT_PREFIX = "APUNTES_"
TRANSCRIPT_PREFIX = "TRANSCRIPCION_"
OUTPUT_PREFIXES = (OUTPUT_PREFIX, TRANSCRIPT_PREFIX)
CHUNK_SECONDS = 45 * 60       # 45 min por bloque (~90k tokens en Hydrogen, bajo 250k TPM)
OVERLAP_SECONDS = 15          # 15s solapamiento entre bloques

FFMPEG_FILTER = (
    "highpass=f=85,lowpass=f=7800,"
    "afftdn=nr=20:nf=-30:tn=1,"
    "equalizer=f=300:t=q:w=1.0:g=-3,"
    "equalizer=f=2800:t=q:w=1.2:g=3,"
    "dynaudnorm=f=150:g=15:p=0.9:m=10,"
    "loudnorm=I=-16:TP=-1.5:LRA=11"
)

RETRY_DELAYS = (5, 10, 15, 20)
MODELS_CHAIN = ("gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite")
VOCAB_MODELS_CHAIN = ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite")
TRIAGE_MODELS_CHAIN = ("gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash")
LARGE_PDF_PAGES = 25
PDF_CHUNK_PAGES = 25
MAX_CHUNK_WORDS_SANE = 18000
LOOP_RETRY_DROPPED_WORDS = 3000
LARGE_TRANSCRIPT_WORDS = 18000
MAX_TOPIC_WORDS = 22000
TRANSCRIBE_MAX_OUTPUT_TOKENS = 20000


def natural_key(path: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


def is_base_notes_pdf(path: Path) -> bool:
    if not (path.is_file() and path.suffix.lower() == ".pdf"):
        return False
    low = path.name.lower()
    return any(low.startswith(pref.lower()) for pref in OUTPUT_PREFIXES)


def fmt_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_mb(size_bytes: int) -> str:
    return f"{size_bytes / (1024 * 1024):.1f} MB"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_protect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buf = ctypes.create_string_buffer(data, len(data))
    in_blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buf = ctypes.create_string_buffer(data, len(data))
    in_blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


class _CLIError(Exception):
    def __init__(self, code: str, message: str, fix: str, exit_code: int, transient: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix
        self.exit_code = exit_code
        self.transient = transient


def emit_json(status: str, data=None, error=None, warnings=None):
    payload: dict = {"status": status}
    if data is not None:
        payload["data"] = data
    if error is not None:
        payload["error"] = error
    if warnings is not None:
        payload["warnings"] = warnings
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _write_error_log(exc: BaseException) -> Path | None:
    try:
        log_path = get_runtime_dir() / "last_error.log"
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        return log_path
    except Exception:
        return None


def emit_error(err: _CLIError, json_mode: bool, target_console: Console):
    if json_mode:
        emit_json(
            "error",
            error={
                "code": err.code,
                "message": err.message,
                "fix": err.fix,
                "transient": err.transient,
            },
        )
    else:
        target_console.print(
            Panel(
                f"{escape(err.message)}\n"
                f"[muted]Solucion: {escape(err.fix)}[/]\n"
                f"[muted]Codigo: {escape(err.code)} (exit {err.exit_code})[/]",
                title=f"[err]Error ({escape(err.code)})[/]",
                box=box.ROUNDED,
                border_style="red",
            )
        )


def build_parser() -> argparse.ArgumentParser:
    examples = (
        "EJEMPLOS:\n"
        "  procesar_clase.bat                   Procesa audios de la carpeta actual\n"
        "  procesar_clase.bat --mode apuntes    Genera apuntes directamente sin preguntar\n"
        "  procesar_clase.bat --keep-md         Guarda tambien el Markdown junto al PDF\n"
        "  procesar_clase.bat --apis            Menu interactivo para gestionar APIs\n"
        "  procesar_clase.bat --dry-run         Muestra archivos detectados sin procesar\n"
        "  procesar_clase.bat --json            Devuelve el resultado en JSON por stdout\n"
        "  procesar_clase.bat --add-api         Anade y comprueba una nueva clave de API\n"
        "  procesar_clase.bat --reset-key       Borra las claves de API guardadas\n"
        "  procesar_clase.bat --debug           Simula todas las fases sin usar API"
    )
    parser = argparse.ArgumentParser(
        prog="procesar_clase.bat",
        description="Convierte audios de clase en apuntes PDF limpios y estructurados.",
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("carpeta", nargs="?", default=None, help="Carpeta con audios y apuntes opcionales (por defecto: carpeta actual)")
    parser.add_argument("--version", action="version", version=f"Clase2PDF {APP_VERSION}")
    parser.add_argument("--debug", action="store_true", help="Simula todas las fases sin gastar API ni escribir archivos")
    parser.add_argument("--dry-run", action="store_true", help="Muestra archivos detectados y plan sin procesar")
    parser.add_argument(
        "--mode",
        choices=("transcripcion", "apuntes"),
        default=None,
        help="Tipo de salida: transcripcion o apuntes (si se omite, pregunta)",
    )
    parser.add_argument("--json", dest="json_out", action="store_true", help="Emite salida estructurada en JSON por stdout")
    parser.add_argument("--keep-md", action="store_true", help="Guarda tambien el archivo Markdown (.md) junto al PDF")
    parser.add_argument("--apis", action="store_true", help="Abre el menu interactivo para gestionar claves de API")
    parser.add_argument("--add-api", action="store_true", help="Anade y verifica una nueva clave de API (API 1, API 2, ...)")
    parser.add_argument("--reset-key", action="store_true", help="Borra las claves de API guardadas antes de ejecutar")
    parser.add_argument("-q", "--quiet", action="store_true", help="Oculta paneles y barras de progreso")
    parser.add_argument("--no-clear", action="store_true", help="No limpia la pantalla al arrancar")
    return parser


def get_runtime_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else (Path.home() / "AppData" / "Local")
    d = root / "Clase2PDF"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hide_dir_windows(path: Path):
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)
        except Exception:
            pass


def _flush_console_input():
    if os.name == "nt":
        try:
            if sys.stdin and sys.stdin.isatty():
                handle = ctypes.windll.kernel32.GetStdHandle(-10)
                if handle and handle != -1:
                    ctypes.windll.kernel32.FlushConsoleInputBuffer(handle)
        except Exception:
            pass


atexit.register(_flush_console_input)


def _sanitize_api_key(raw: str) -> str:
    cleaned = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", raw)
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", cleaned)
    return cleaned.strip().strip('"').strip("'")


def _is_valid_api_key_format(key: str) -> bool:
    return len(key) >= 16 and key.isascii() and not any(ch.isspace() for ch in key)


def _api_key_path(runtime_dir: Path, slot: int) -> Path:
    return runtime_dir / ("api_key.dpapi" if slot == 1 else f"api_key_{slot}.dpapi")


def list_saved_api_keys(runtime_dir: Path) -> list[tuple[int, Path, str]]:
    candidates: list[tuple[int, Path]] = []
    primary = runtime_dir / "api_key.dpapi"
    if primary.exists():
        candidates.append((1, primary))
    for p in runtime_dir.glob("api_key_*.dpapi"):
        m = re.fullmatch(r"api_key_(\d+)\.dpapi", p.name)
        if m:
            slot = int(m.group(1))
            if slot >= 2:
                candidates.append((slot, p))
    candidates.sort(key=lambda item: item[0])

    valid: list[tuple[int, Path, str]] = []
    for slot, path in candidates:
        try:
            key = _sanitize_api_key(_dpapi_unprotect(path.read_bytes()).decode("utf-8"))
            if _is_valid_api_key_format(key):
                valid.append((slot, path, key))
        except Exception:
            pass
    return valid


def _next_api_slot(runtime_dir: Path) -> int:
    existing = list_saved_api_keys(runtime_dir)
    if not any(slot == 1 for slot, _, _ in existing):
        return 1
    return max(slot for slot, _, _ in existing) + 1


def _clear_saved_api_keys(runtime_dir: Path, legacy_pipe_dir: Path | None = None):
    (runtime_dir / "api_key.dpapi").unlink(missing_ok=True)
    for p in runtime_dir.glob("api_key_*.dpapi"):
        p.unlink(missing_ok=True)
    if legacy_pipe_dir is not None:
        (legacy_pipe_dir / "api_key.dpapi").unlink(missing_ok=True)


def _prompt_api_key_interactive(
    title: str = "Clave de API",
    subtitle: str = "Introduce tu clave de API de Google (pega con Ctrl+V o clic derecho y pulsa Enter).\nSe guardara cifrada en este equipo.",
) -> str:
    console.print(
        Panel(
            subtitle,
            title=f"[accent]{escape(title)}[/]",
            box=box.ROUNDED,
            border_style="cyan",
        )
    )
    key = ""
    for attempt in range(3):
        _flush_console_input()
        console.print("[accent]Clave:[/] ", end="")
        try:
            raw = input()
        except EOFError:
            raw = ""
        _flush_console_input()
        key = _sanitize_api_key(raw)
        if _is_valid_api_key_format(key):
            break
        if attempt < 2:
            console.print("[warn]La clave no parece valida (vacia, muy corta o con espacios). Pega solo la clave y pulsa Enter.[/]")

    if not _is_valid_api_key_format(key):
        raise _CLIError(
            code="EMPTY_API_KEY" if not key else "INVALID_API_KEY_FORMAT",
            message="La clave de API no es valida.",
            fix="Copia solo la clave (empieza por AQ. o AIza...) sin espacios y vuelve a ejecutar.",
            exit_code=EXIT_CONFIG,
            transient=False,
        )

    if sys.stdout.isatty():
        try:
            sys.stdout.write("\x1b[1A\x1b[2K")
            sys.stdout.flush()
        except Exception:
            pass
    return key


def test_api_key(key: str) -> tuple[bool, str]:
    try:
        genai, types = _load_genai()
        client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(api_version="v1beta"),
        )
        next(iter(client.models.list(config={"page_size": 1})), None)
        return True, ""
    except Exception as exc:
        msg = (str(exc).splitlines() or ["Error desconocido"])[0].strip()
        return False, msg[:180]


def add_api_key_flow(runtime_dir: Path, non_interactive: bool = False, json_mode: bool = False) -> dict:
    if non_interactive:
        raise _CLIError(
            code="NON_INTERACTIVE",
            message="No se puede pedir clave sin terminal interactiva.",
            fix="Ejecuta --add-api desde una consola interactiva.",
            exit_code=EXIT_CONFIG,
            transient=False,
        )

    existing = list_saved_api_keys(runtime_dir)
    slot = _next_api_slot(runtime_dir)
    if not json_mode:
        if existing:
            labels = ", ".join(f"API {s}" for s, _, _ in existing)
            console.print(f"[muted]Claves guardadas: {len(existing)} ({labels})[/]")
        else:
            console.print("[muted]Claves guardadas: ninguna[/]")

    key = _prompt_api_key_interactive(
        title=f"Añadir API {slot}",
        subtitle=(
            f"Introduce tu clave de API {slot} (pega con Ctrl+V o clic derecho y pulsa Enter).\n"
            "Se comprobara antes de guardarse cifrada en este equipo."
        ),
    )

    for ex_slot, _, ex_key in existing:
        if ex_key == key:
            if not json_mode:
                console.print(f"[ok]✓[/] Esa clave ya estaba guardada como API {ex_slot}.")
            return {"slot": ex_slot, "duplicate": True, "verified": True, "total_keys": len(existing)}

    if not json_mode:
        console.print(f"[accent]Comprobando API {slot}...[/]")
    ok, reason = test_api_key(key)
    if not ok:
        raise _CLIError(
            code="API_KEY_TEST_FAILED",
            message=f"La clave no paso la prueba: {reason}",
            fix="Revisa la clave en Google AI Studio y vuelve a intentarlo.",
            exit_code=EXIT_AUTH,
            transient=False,
        )

    dst = _api_key_path(runtime_dir, slot)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(_dpapi_protect(key.encode("utf-8")))
    if not json_mode:
        console.print(f"[ok]✓[/] API {slot} verificada y guardada.")
    return {"slot": slot, "duplicate": False, "verified": True, "total_keys": len(existing) + 1}


def manage_api_keys_menu(runtime_dir: Path, legacy_pipe_dir: Path | None = None):
    """Menu interactivo (sin CMD) para ver, anadir, probar o borrar claves de API."""
    import questionary
    from questionary import Choice, Style

    style = Style(
        [
            ("qmark", "fg:#00d7ff bold"),
            ("question", "bold"),
            ("answer", "fg:#00ff87 bold"),
            ("pointer", "fg:#00d7ff bold"),
            ("highlighted", "fg:#00d7ff bold"),
            ("selected", "fg:#00ff87 bold noreverse"),
            ("instruction", "fg:#808080"),
        ]
    )
    while True:
        saved = list_saved_api_keys(runtime_dir)
        labels = ", ".join(f"API {s}" for s, _, _ in saved) if saved else "ninguna"
        console.print(f"\n[accent]Claves guardadas:[/] {len(saved)} [muted]({labels})[/]")
        action = questionary.select(
            "Gestion de claves de API",
            choices=[
                Choice("Añadir nueva clave de API", value="add"),
                Choice("Probar claves guardadas", value="test"),
                Choice("Eliminar una clave concreta", value="delete_one"),
                Choice("Borrar todas las claves", value="clear"),
                Choice("Volver", value="back"),
            ],
            instruction="(Usa ↑/↓ y pulsa Enter)",
            style=style,
        ).ask()
        if action in (None, "back"):
            break
        if action == "add":
            try:
                add_api_key_flow(runtime_dir, non_interactive=False, json_mode=False)
            except _CLIError as err:
                emit_error(err, False, console)
        elif action == "test":
            if not saved:
                console.print("[warn]No hay claves guardadas para probar.[/]")
                continue
            for slot, _, k in saved:
                ok, reason = test_api_key(k)
                if ok:
                    console.print(f"[ok]✓[/] API {slot}: OK")
                else:
                    console.print(f"[err]✗[/] API {slot}: {escape(reason)}")
        elif action == "delete_one":
            if not saved:
                console.print("[muted]No hay claves guardadas.[/]")
                continue
            target_path = questionary.select(
                "¿Qué clave quieres eliminar?",
                choices=[Choice(f"API {s}", value=p) for s, p, _ in saved] + [Choice("Cancelar", value=None)],
                style=style,
            ).ask()
            if target_path is not None:
                target_path.unlink(missing_ok=True)
                console.print("[ok]✓[/] Clave eliminada.")
        elif action == "clear":
            if not saved:
                console.print("[muted]No hay claves guardadas.[/]")
                continue
            if questionary.confirm("¿Seguro que quieres borrar todas las claves guardadas?", default=False, style=style).ask():
                _clear_saved_api_keys(runtime_dir, legacy_pipe_dir)
                console.print("[ok]✓[/] Claves borradas.")


def get_or_prompt_api_key(runtime_dir: Path, non_interactive: bool = False, legacy_pipe_dir: Path | None = None) -> str:
    env_key = _sanitize_api_key(os.environ.get("GEMINI_API_KEY", ""))
    if env_key:
        return env_key

    dpapi_file = runtime_dir / "api_key.dpapi"
    if not dpapi_file.exists() and legacy_pipe_dir is not None:
        legacy_file = legacy_pipe_dir / "api_key.dpapi"
        if legacy_file.exists():
            try:
                raw = legacy_file.read_bytes()
                key = _sanitize_api_key(_dpapi_unprotect(raw).decode("utf-8"))
                if _is_valid_api_key_format(key):
                    runtime_dir.mkdir(parents=True, exist_ok=True)
                    dpapi_file.write_bytes(raw)
                    legacy_file.unlink(missing_ok=True)
                    return key
            except Exception:
                pass

    if dpapi_file.exists():
        try:
            key = _sanitize_api_key(_dpapi_unprotect(dpapi_file.read_bytes()).decode("utf-8"))
            if _is_valid_api_key_format(key):
                return key
            console.print("[warn]La clave guardada estaba dañada. Introduce una nueva.[/]")
            dpapi_file.unlink(missing_ok=True)
        except Exception:
            console.print("[warn]No se pudo leer la clave guardada. Introduce una nueva.[/]")
            dpapi_file.unlink(missing_ok=True)

    saved = list_saved_api_keys(runtime_dir)
    if saved:
        return saved[0][2]

    if non_interactive:
        raise _CLIError(
            code="MISSING_API_KEY",
            message="No hay clave de API configurada.",
            fix="Define la variable de entorno GEMINI_API_KEY o ejecuta en una terminal interactiva.",
            exit_code=EXIT_CONFIG,
            transient=False,
        )

    key = _prompt_api_key_interactive()
    dpapi_file.parent.mkdir(parents=True, exist_ok=True)
    dpapi_file.write_bytes(_dpapi_protect(key.encode("utf-8")))
    if sys.stdout.isatty():
        console.print("[ok]✓[/] Clave guardada de forma segura.")
    return key


def get_or_prompt_api_keys(
    runtime_dir: Path,
    non_interactive: bool = False,
    legacy_pipe_dir: Path | None = None,
) -> list[tuple[int, str]]:
    env_key = _sanitize_api_key(os.environ.get("GEMINI_API_KEY", ""))
    if env_key:
        return [(1, env_key)]

    primary = get_or_prompt_api_key(
        runtime_dir, non_interactive=non_interactive, legacy_pipe_dir=legacy_pipe_dir
    )
    saved = [(slot, k) for slot, _, k in list_saved_api_keys(runtime_dir)]
    return saved if saved else [(1, primary)]


class ClientPool:
    """Pool de clientes Gemini por API key con rotacion sticky por modelo."""

    def __init__(self, keys: list[tuple[int, str]], client_factory=None):
        if not keys:
            raise ValueError("ClientPool requiere al menos una clave")
        self._keys = list(keys)
        self._factory = client_factory
        self._clients: dict[int, object] = {}
        self.current_idx = 0
        self.exhausted_pairs: set[tuple[int, str]] = set()
        self.invalid_keys: set[int] = set()

    @property
    def size(self) -> int:
        return len(self._keys)

    def current_slot(self) -> int:
        return self._keys[self.current_idx][0]

    def get_client(self, idx: int | None = None):
        i = self.current_idx if idx is None else idx
        if i not in self._clients:
            _, key = self._keys[i]
            if self._factory is not None:
                self._clients[i] = self._factory(key)
            else:
                genai, types = _load_genai()
                self._clients[i] = genai.Client(
                    api_key=key,
                    http_options=types.HttpOptions(api_version="v1beta"),
                )
        return self._clients[i]

    def candidate_indices(self, model: str) -> list[int]:
        n = len(self._keys)
        order = [(self.current_idx + off) % n for off in range(n)]
        return [
            i for i in order
            if i not in self.invalid_keys and (i, model) not in self.exhausted_pairs
        ]

    def mark_exhausted(self, idx: int, model: str):
        self.exhausted_pairs.add((idx, model))

    def mark_invalid(self, idx: int):
        self.invalid_keys.add(idx)

    def select(self, idx: int):
        self.current_idx = idx


def _upload_local_file(client, path: Path, on_status=None):
    """Sube archivo como stream binario con mime_type explicito y reintento ante cortes de red."""
    ext = path.suffix.lower()
    mime_map = {
        ".pdf": "application/pdf",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".ogg": "audio/ogg",
        ".opus": "audio/opus",
        ".flac": "audio/flac",
        ".webm": "audio/webm",
        ".aac": "audio/aac",
    }
    mime_type = mime_map.get(ext) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"

    def _do_upload():
        with path.open("rb") as fh:
            return client.files.upload(file=fh, config={"mime_type": mime_type})

    return call_with_retry(_do_upload, f"Subida ({path.name})", on_status=on_status)


def wait_file_active(client: genai.Client, uploaded_file, on_status=None):
    while getattr(uploaded_file, "state", None) and uploaded_file.state.name == "PROCESSING":
        time.sleep(2)
        uploaded_file = call_with_retry(
            lambda: client.files.get(name=uploaded_file.name),
            "Estado archivo",
            on_status=on_status,
        )
    if getattr(uploaded_file, "state", None) and uploaded_file.state.name == "FAILED":
        raise RuntimeError(f"Error procesando archivo remoto: {uploaded_file.name}")
    return uploaded_file


def _classify_error(exc: Exception) -> str:
    msg = f"{exc.__class__.__name__} {exc}".lower()
    if "404" in msg or "not_found" in msg:
        return "model_not_found"
    if any(
        tok in msg
        for tok in (
            "api_key_invalid",
            "invalid api key",
            "api key not valid",
            "illegal header",
            "invalid header",
            "401",
        )
    ):
        return "auth_error"
    if ("429" in msg or "resource_exhausted" in msg) and any(
        tok in msg for tok in ("perday", "per_day", "requestsperday", "daily")
    ):
        return "quota_daily"
    if "429" in msg or "resource_exhausted" in msg:
        return "rate_limit"
    if any(tok in msg for tok in ("500", "502", "503", "504", "unavailable", "high demand", "overloaded")):
        return "server_busy"
    if any(
        tok in msg
        for tok in (
            "remoteprotocolerror",
            "server disconnected",
            "readtimeout",
            "connecterror",
            "connecttimeout",
            "connectionreset",
            "brokenpipe",
            "timeouterror",
            "timed out",
            "connection aborted",
            "incomplete read",
            "eof occurred",
        )
    ):
        return "transient"
    return "fatal"


def _short_reason(exc: Exception) -> str:
    msg = str(exc)
    for code in ("503", "429", "404"):
        if code in msg:
            return code
    return exc.__class__.__name__


def _transient_reason(exc: Exception) -> str:
    kind = _classify_error(exc)
    if kind == "quota_daily":
        return "429 cuota diaria agotada"
    msg = f"{exc.__class__.__name__} {exc}".lower()
    if ("429" in msg or "resource_exhausted" in msg) and "token" in msg and ("minute" in msg or "tpm" in msg):
        return "429 limite de tokens por minuto (TPM)"
    if "429" in msg or "resource_exhausted" in msg:
        return "429 limite por minuto / cuota"
    if any(tok in msg for tok in ("503", "high demand", "overloaded", "unavailable")):
        return "503 servidores de Google saturados"
    if any(tok in msg for tok in ("500", "502", "504")):
        return "5xx error temporal del servidor"
    if any(
        tok in msg
        for tok in (
            "remoteprotocolerror",
            "server disconnected",
            "readtimeout",
            "connecterror",
            "connecttimeout",
            "connectionreset",
            "brokenpipe",
            "timeouterror",
            "timed out",
            "connection aborted",
            "incomplete read",
            "eof occurred",
        )
    ):
        return "corte de red"
    return exc.__class__.__name__


def call_with_retry(fn, label: str, delays=RETRY_DELAYS, on_status=None, *, retry_rate_limit: bool = True):
    """Reintenta solo errores transitorios (503 alta demanda, 429 RPM/TPM, cortes de red). Salta directo si es RPD o 404."""
    for idx in range(len(delays) + 1):
        try:
            return fn()
        except Exception as exc:
            kind = _classify_error(exc)
            if (kind in ("transient", "server_busy") or (kind == "rate_limit" and retry_rate_limit)) and idx < len(delays):
                wait = delays[idx]
                msg = f"[warn]Reintentando ({idx + 1}/{len(delays)}) en {wait}s...[/]"
                if on_status:
                    on_status(msg)
                else:
                    console.print(f"  -> {label}: {msg}")
                time.sleep(wait)
                continue
            raise


def _config_for_model(config, model: str):
    if config is None or model == "gemini-4-hydrogen":
        return config
    extra = {}
    for attr in ("max_output_tokens", "temperature"):
        val = getattr(config, attr, None)
        if val is not None:
            extra[attr] = val
    if not extra:
        return None
    _, types = _load_genai()
    return types.GenerateContentConfig(**extra)


def generate_with_fallback(
    pool: ClientPool,
    contents,
    config,
    label: str,
    on_status=None,
    file_paths: list[Path] | None = None,
    preuploaded: tuple[int, list] | None = None,
    models_chain: tuple[str, ...] = MODELS_CHAIN,
):
    """Prioriza mantener el mismo modelo rotando entre APIs; solo baja de modelo si todas agotan cuota."""
    last_exc = None
    for m_idx, model in enumerate(models_chain):
        cfg = _config_for_model(config, model)
        candidates = pool.candidate_indices(model)
        for c_pos, key_idx in enumerate(candidates):
            client = pool.get_client(key_idx)
            uploaded_here: list = []
            try:
                if preuploaded is not None and preuploaded[0] == key_idx:
                    remote_files = list(preuploaded[1])
                elif file_paths:
                    for fp in file_paths:
                        f = _upload_local_file(client, fp, on_status=on_status)
                        uploaded_here.append(wait_file_active(client, f, on_status=on_status))
                    remote_files = uploaded_here
                else:
                    remote_files = []

                base_items = list(contents) if isinstance(contents, list) else [contents]
                call_contents = [*remote_files, *base_items] if remote_files else contents

                resp = call_with_retry(
                    lambda c=client, m=model, cc=call_contents, cf=cfg: c.models.generate_content(
                        model=m,
                        contents=cc,
                        config=cf,
                    ),
                    f"{label} [{model}]",
                    on_status=on_status,
                    retry_rate_limit=(c_pos + 1 >= len(candidates)),
                )
                pool.select(key_idx)
                return resp, model
            except Exception as exc:
                last_exc = exc
                kind = _classify_error(exc)
                if kind == "fatal":
                    raise
                if kind in ("model_not_found", "server_busy"):
                    break
                if kind == "auth_error":
                    pool.mark_invalid(key_idx)
                elif kind == "quota_daily":
                    pool.mark_exhausted(key_idx, model)
                if c_pos + 1 < len(candidates):
                    pool.select(candidates[c_pos + 1])
                    msg = "[warn]Cambiando de clave...[/]"
                    if on_status:
                        on_status(msg)
                    else:
                        console.print(f"  -> {label}: {msg}")
                    continue
            finally:
                for uf in uploaded_here:
                    try:
                        client.files.delete(name=uf.name)
                    except Exception:
                        pass

        if m_idx + 1 < len(models_chain) and last_exc is not None:
            msg = "[warn]Cambiando de modelo...[/]"
            if on_status:
                on_status(msg)
            else:
                console.print(f"  -> {label}: {msg}")

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("No hay claves o modelos disponibles.")


class PipelineUI:
    """Tabla de estado en vivo + barra de progreso unificada sin scroll spam."""

    def __init__(self, target_console: Console):
        self.console = target_console
        self._lock = threading.Lock()
        self._rows: list[list[str]] = []
        self._index: dict[str, int] = {}
        self._live: Live | None = None
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[accent]{task.description}[/]"),
            BarColumn(bar_width=None),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=target_console,
            expand=True,
        )
        self.task_id = self.progress.add_task("Preparando...", total=1)

    def attach_live(self, live: Live):
        with self._lock:
            self._live = live

    def add_row(self, key: str, label: str, status: str = "[muted]○ Pendiente[/]", detail: str = ""):
        with self._lock:
            if key not in self._index:
                self._index[key] = len(self._rows)
                self._rows.append([key, label, status, detail])
            if self._live:
                self._live.update(self._build_renderable())

    def update_row(self, key: str, status: str | None = None, detail: str | None = None):
        with self._lock:
            pos = self._index.get(key)
            if pos is not None:
                if status is not None:
                    self._rows[pos][2] = status
                if detail is not None:
                    self._rows[pos][3] = detail
            if self._live:
                self._live.update(self._build_renderable())

    def set_progress(self, description: str | None = None, advance: float = 0, total: float | None = None, completed: float | None = None):
        with self._lock:
            kwargs = {}
            if description is not None:
                kwargs["description"] = description
            if total is not None:
                kwargs["total"] = total
            if completed is not None:
                kwargs["completed"] = completed
            if kwargs:
                self.progress.update(self.task_id, **kwargs)
            if advance:
                self.progress.advance(self.task_id, advance)
            if self._live:
                self._live.update(self._build_renderable())

    def _build_renderable(self) -> Group:
        table = Table(box=box.SIMPLE_HEAVY, expand=True, show_edge=False)
        table.add_column("Tarea", style="bold", ratio=3, no_wrap=True, overflow="ellipsis")
        table.add_column("Estado", ratio=2, no_wrap=True)
        table.add_column("Info", style="muted", ratio=2, no_wrap=True, overflow="ellipsis")
        for _, label, status, detail in self._rows:
            table.add_row(label, status, detail)
        return Group(table, self.progress)

    def render(self) -> Group:
        with self._lock:
            return self._build_renderable()


def render_intro(
    root_dir: Path,
    audios: list[Path],
    docs: list[Path],
    max_workers: int,
    target_console: Console = console,
    base_notes: list[Path] | None = None,
):
    tree = Tree("[accent]Archivos[/]")
    audio_branch = tree.add(f"[stage]Audios ({len(audios)})[/]")
    for a in audios:
        audio_branch.add(f"{escape(a.name)} [muted]({fmt_mb(a.stat().st_size)})[/]")

    if base_notes:
        base_branch = tree.add(f"[stage]Apuntes base ({len(base_notes)})[/]")
        for b in base_notes:
            base_branch.add(f"{escape(b.name)} [muted]({fmt_mb(b.stat().st_size)})[/]")

    if docs:
        doc_branch = tree.add(f"[stage]Apuntes ({len(docs)})[/]")
        for d in docs:
            doc_branch.add(f"{escape(d.name)} [muted]({fmt_mb(d.stat().st_size)})[/]")

    target_console.print(
        Panel(
            tree,
            title="[accent]Clase2PDF[/]",
            subtitle=f"[muted]{escape(root_dir.name or str(root_dir))}[/]",
            box=box.ROUNDED,
            border_style="cyan",
            padding=(1, 2),
        )
    )


def resolve_output_mode(
    cli_mode: str | None,
    non_interactive: bool = False,
    json_mode: bool = False,
    runtime_dir: Path | None = None,
    legacy_pipe_dir: Path | None = None,
) -> str:
    if cli_mode in ("transcripcion", "apuntes"):
        return cli_mode
    if non_interactive or json_mode:
        return "transcripcion"

    try:
        import questionary
        from questionary import Choice, Style

        style = Style(
            [
                ("qmark", "fg:#00d7ff bold"),
                ("question", "bold"),
                ("answer", "fg:#00ff87 bold"),
                ("pointer", "fg:#00d7ff bold"),
                ("highlighted", "fg:#00d7ff bold"),
                ("selected", "fg:#00ff87 bold noreverse"),
                ("instruction", "fg:#808080"),
            ]
        )
        while True:
            choices = [
                Choice("Transcripción completa de la clase", value="transcripcion"),
                Choice("Apuntes redactados para estudiar", value="apuntes"),
            ]
            if runtime_dir is not None:
                n_keys = len(list_saved_api_keys(runtime_dir))
                choices.append(Choice(f"Gestionar claves de API ({n_keys} guardadas)", value="manage_apis"))

            ans = questionary.select(
                "¿Qué resultado quieres generar?",
                choices=choices,
                default="transcripcion",
                instruction="(Usa ↑/↓ y pulsa Enter)",
                style=style,
            ).ask()
            if ans is None:
                raise KeyboardInterrupt
            if ans == "manage_apis" and runtime_dir is not None:
                manage_api_keys_menu(runtime_dir, legacy_pipe_dir)
                continue
            return ans
    except ImportError:
        console.print("[accent]1.[/] Transcripción completa de la clase")
        console.print("[accent]2.[/] Apuntes redactados para estudiar")
        choice = Prompt.ask(
            "Elige modo",
            choices=["1", "2"],
            default="1",
            console=console,
        )
        return "apuntes" if choice == "2" else "transcripcion"


def render_summary(
    pdf_out: Path | list[Path],
    audios: list[Path],
    chunks: list[Path],
    vocab: list[str],
    models_used: list[str],
    elapsed: float,
    target_console: Console = console,
    total_audio_sec: float = 0.0,
    mode: str | None = None,
):
    pdfs = list(pdf_out) if isinstance(pdf_out, list) else [pdf_out]
    table = Table(box=box.SIMPLE, show_header=False, expand=True)
    table.add_column("Campo", style="muted", width=12)
    table.add_column("Valor")
    if len(pdfs) == 1:
        size_kb = pdfs[0].stat().st_size / 1024
        table.add_row("Archivo", f"[ok]{escape(pdfs[0].name)}[/] [muted]({size_kb:.0f} KB)[/]")
    else:
        lines = [f"[ok]{escape(p.name)}[/] [muted]({p.stat().st_size / 1024:.0f} KB)[/]" for p in pdfs]
        table.add_row("Archivos", "\n".join(lines))
    if mode:
        table.add_row("Modo", "Apuntes" if mode == "apuntes" else "Transcripcion")
    table.add_row("Carpeta", f"[muted]{escape(str(pdfs[0].parent))}[/]")
    audios_str = f"{len(audios)} ({fmt_duration(total_audio_sec)})" if total_audio_sec > 0 else str(len(audios))
    table.add_row("Audios", audios_str)
    table.add_row("Tiempo", fmt_duration(elapsed))

    target_console.print(
        Panel(
            table,
            title="[ok]✓ Listo[/]",
            box=box.ROUNDED,
            border_style="green",
            padding=(0, 1),
        )
    )


def read_docx_text(path: Path, max_chars: int = 80000) -> str:
    """Extrae texto plano de un .docx en Python puro sin dependencias externas."""
    try:
        with zipfile.ZipFile(path) as zf:
            xml_bytes = zf.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError):
        return ""
    paragraphs: list[str] = []
    for elem in root.iter():
        if elem.tag.endswith("}p") or elem.tag == "p":
            texts = [
                sub.text for sub in elem.iter()
                if (sub.tag.endswith("}t") or sub.tag == "t") and sub.text
            ]
            line = "".join(texts).strip()
            if line:
                paragraphs.append(line)
    return "\n".join(paragraphs)[:max_chars]


_PDF_PAGES_CACHE: dict[tuple[str, int, int], list[str]] = {}


def inspect_pdf_pages(pdf_path: Path) -> list[str]:
    """Devuelve texto por pagina (1 item por pagina) usando pypdf con cache en memoria."""
    try:
        st = pdf_path.stat()
        key = (str(pdf_path.resolve()), st.st_mtime_ns, st.st_size)
        if key in _PDF_PAGES_CACHE:
            return _PDF_PAGES_CACHE[key]
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        _PDF_PAGES_CACHE[key] = pages
        return pages
    except Exception:
        return []


def is_large_pdf(path: Path) -> bool:
    return path.suffix.lower() == ".pdf" and len(inspect_pdf_pages(path)) > LARGE_PDF_PAGES


def parse_page_ranges(raw_ranges, max_page: int) -> list[tuple[int, int]]:
    """Normaliza rangos 1-indexados [[inicio, fin], ...] ordenados y fusionados."""
    if not isinstance(raw_ranges, list) or max_page <= 0:
        return []
    cleaned: list[tuple[int, int]] = []
    for item in raw_ranges:
        if isinstance(item, int):
            a = b = item
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                a, b = int(item[0]), int(item[1])
            except (TypeError, ValueError):
                continue
        else:
            continue
        if a > b:
            a, b = b, a
        a = max(1, min(a, max_page))
        b = max(1, min(b, max_page))
        cleaned.append((a, b))
    if not cleaned:
        return []
    cleaned.sort()
    merged: list[tuple[int, int]] = [cleaned[0]]
    for a, b in cleaned[1:]:
        la, lb = merged[-1]
        if a <= lb + 1:
            merged[-1] = (la, max(lb, b))
        else:
            merged.append((a, b))
    return merged


def slice_pdf_pages(pdf_path: Path, page_ranges: list[tuple[int, int]], out_path: Path) -> Path:
    """Corta literalmente las paginas indicadas a out_path sin modificar el contenido."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    total = len(reader.pages)
    seen: set[int] = set()
    for start, end in page_ranges:
        for p_idx in range(max(1, start) - 1, min(total, end)):
            if p_idx not in seen:
                seen.add(p_idx)
                writer.add_page(reader.pages[p_idx])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as fh:
        writer.write(fh)
    return out_path


def split_pdf_into_chunks(pdf_path: Path, chunk_size: int, out_dir: Path) -> list[tuple[int, int, Path]]:
    """Divide un PDF en mini-PDFs de chunk_size paginas dentro de out_dir."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: list[tuple[int, int, Path]] = []
    for start in range(0, total, chunk_size):
        end = min(total, start + chunk_size)
        writer = PdfWriter()
        for p_idx in range(start, end):
            writer.add_page(reader.pages[p_idx])
        part_path = out_dir / f"{pdf_path.stem}_p{start + 1}_{end}.pdf"
        with part_path.open("wb") as fh:
            writer.write(fh)
        parts.append((start + 1, end, part_path))
    return parts


def build_pdf_outline_digest(pages_text: list[str], max_total_chars: int = 100000) -> str:
    if not pages_text:
        return ""
    per_page = min(400, max(80, max_total_chars // max(1, len(pages_text))))
    lines: list[str] = []
    for i, txt in enumerate(pages_text, 1):
        compact = re.sub(r"\s+", " ", txt).strip()[:per_page]
        lines.append(f"[PAG {i}] {compact or '(sin texto)'}")
    return "\n".join(lines)[:max_total_chars]


def _sample_transcripts(transcripts: list[str], max_chars: int = 24000) -> str:
    joined = "\n\n".join(transcripts).strip()
    if len(joined) <= max_chars:
        return joined
    part = max_chars // 3
    mid = len(joined) // 2
    return (
        f"{joined[:part]}\n\n[...]\n\n"
        f"{joined[max(0, mid - part // 2):mid + part // 2]}\n\n[...]\n\n"
        f"{joined[-part:]}"
    )


def _extract_json_object(raw: str) -> dict | None:
    text = strip_markdown_fences(raw)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def extract_vocabulary(
    pool: ClientPool,
    docs: list[Path],
    pipe_dir: Path,
    ui: PipelineUI,
) -> tuple[list[str], str | None]:
    if not docs:
        return [], None

    cache_file = pipe_dir / "vocab_cache.json"
    sig = {"v": 2, "files": {p.name: p.stat().st_mtime for p in docs}}
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("signature") == sig and isinstance(cached.get("terms"), list):
                terms = cached["terms"]
                ui.update_row("vocab", "[ok]✓ Listo[/]", f"{len(terms)} terminos")
                return terms, None
        except Exception:
            pass

    ui.update_row("vocab", "[accent]Leyendo...[/]", "")
    raw_batches: list[tuple[str, str | list[Path]]] = []
    slices_dir = pipe_dir / "slices"

    for doc in docs:
        if doc.suffix.lower() == ".docx":
            txt = read_docx_text(doc, max_chars=240000)
            for off in range(0, len(txt), 60000):
                piece = txt[off:off + 60000].strip()
                if piece:
                    raw_batches.append(("text", f"=== DOCUMENTO: {doc.name} ===\n{piece}"))
        else:
            pages = inspect_pdf_pages(doc)
            avg_chars = (sum(len(p) for p in pages) / len(pages)) if pages else 0
            if pages and avg_chars >= 40:
                for start in range(0, len(pages), PDF_CHUNK_PAGES):
                    chunk_pages = pages[start:start + PDF_CHUNK_PAGES]
                    chunk_txt = "\n\n".join(
                        f"[Pag {start + idx + 1}]\n{pt}"
                        for idx, pt in enumerate(chunk_pages) if pt
                    ).strip()
                    if chunk_txt:
                        for off in range(0, len(chunk_txt), 60000):
                            raw_batches.append(
                                ("text", f"=== PDF: {doc.name} (pags {start + 1}-{start + len(chunk_pages)}) ===\n{chunk_txt[off:off + 60000]}")
                            )
            elif len(pages) > PDF_CHUNK_PAGES:
                for _, _, part_path in split_pdf_into_chunks(doc, PDF_CHUNK_PAGES, slices_dir):
                    raw_batches.append(("pdf", [part_path]))
            else:
                raw_batches.append(("pdf", [doc]))

    # Agrupar bloques pequeños de texto hasta 60k chars
    grouped: list[tuple[str, list[str] | list[Path]]] = []
    cur_texts: list[str] = []
    cur_len = 0
    for kind, payload in raw_batches:
        if kind == "text":
            assert isinstance(payload, str)
            if cur_texts and cur_len + len(payload) > 60000:
                grouped.append(("text", cur_texts))
                cur_texts = []
                cur_len = 0
            cur_texts.append(payload)
            cur_len += len(payload)
        else:
            if cur_texts:
                grouped.append(("text", cur_texts))
                cur_texts = []
                cur_len = 0
            grouped.append(("pdf", payload))  # type: ignore[arg-type]
    if cur_texts:
        grouped.append(("text", cur_texts))

    if not grouped:
        ui.update_row("vocab", "[ok]✓ Listo[/]", "0 terminos")
        return [], None

    prompt = (
        "Extrae hasta 100 terminos tecnicos, nombres propios, taxones, acronimos y "
        "conceptos clave de este material de clase para usarlos como vocabulario "
        "de reconocimiento de voz. Devuelve UNICAMENTE un array JSON de strings, sin markdown."
    )
    all_terms: list[str] = []
    seen_low: set[str] = set()
    used_model: str | None = None
    total_b = len(grouped)

    for b_idx, (kind, payload) in enumerate(grouped, 1):
        if total_b > 1:
            ui.update_row("vocab", f"[accent]Leyendo ({b_idx}/{total_b})...[/]", "")
        contents = [*payload, prompt] if kind == "text" else prompt
        file_paths = payload if kind == "pdf" else None
        resp, model_name = generate_with_fallback(
            pool,
            contents,
            None,
            "Apuntes",
            on_status=lambda msg: ui.update_row("vocab", msg),
            file_paths=file_paths,  # type: ignore[arg-type]
            models_chain=VOCAB_MODELS_CHAIN,
        )
        used_model = model_name
        raw = (resp.text or "").strip()
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        try:
            batch_terms = json.loads(match.group(0)) if match else []
        except (json.JSONDecodeError, ValueError):
            batch_terms = []
        if not isinstance(batch_terms, list):
            batch_terms = []
        for t in batch_terms:
            s = str(t).strip()
            if s and s.lower() not in seen_low:
                seen_low.add(s.lower())
                all_terms.append(s)

    terms = all_terms[:120]
    cache_file.write_text(
        json.dumps({"signature": sig, "terms": terms}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ui.update_row("vocab", "[ok]✓ Listo[/]", f"{len(terms)} terminos")
    return terms, used_model


def select_and_slice_docs(
    pool: ClientPool,
    docs: list[Path],
    transcripts: list[str],
    pipe_dir: Path,
    ui: PipelineUI,
    row_key: str = "triage",
) -> tuple[list[Path], str, list[str]]:
    """Filtra documentos relevantes (si hay >=2) y acota PDFs grandes por rango exacto de paginas con modelo ligero (0% resumen)."""
    if not docs:
        return [], "", []

    pages_map: dict[Path, list[str]] = {}
    for d in docs:
        if d.suffix.lower() == ".pdf":
            pages_map[d] = inspect_pdf_pages(d)

    needs_filter = len(docs) >= 2
    any_large = any(len(pages_map.get(d, [])) > LARGE_PDF_PAGES for d in docs)
    if not needs_filter and not any_large:
        return list(docs), "", []

    ui.update_row(row_key, "[accent]Filtrando...[/]", "")
    tx_sample = _sample_transcripts(transcripts)
    used_models: list[str] = []
    selected: list[Path] = list(docs)

    # 1. Filtro multi-documento
    if needs_filter:
        catalog_parts: list[str] = []
        small_scanned: list[Path] = []
        for d in docs:
            if d.suffix.lower() == ".docx":
                snippet = read_docx_text(d, max_chars=5000)
                catalog_parts.append(f"--- ARCHIVO: {d.name} (DOCX) ---\n{snippet}")
            else:
                pgs = pages_map.get(d, [])
                avg = (sum(len(p) for p in pgs) / len(pgs)) if pgs else 0
                if pgs and avg >= 40:
                    digest = build_pdf_outline_digest(pgs, max_total_chars=min(25000, 90000 // max(1, len(docs))))
                    catalog_parts.append(f"--- ARCHIVO: {d.name} ({len(pgs)} paginas) ---\n{digest}")
                else:
                    catalog_parts.append(f"--- ARCHIVO: {d.name} ({len(pgs)} paginas, PDF visual/escaneado) ---")
                    if len(pgs) <= LARGE_PDF_PAGES:
                        small_scanned.append(d)

        filter_prompt = (
            "Tienes una transcripcion de clase y varios documentos de la carpeta.\n"
            "Elige UNICAMENTE los archivos cuyo tema tenga relacion directa o continuidad con la transcripcion.\n"
            'Devuelve SOLO JSON valido con este formato exacto: {"relevant": ["nombre1.pdf"]}\n\n'
            f"TRANSCRIPCION (muestra):\n{tx_sample}\n\n"
            "DOCUMENTOS DISPONIBLES:\n" + "\n\n".join(catalog_parts)
        )
        resp, m_name = generate_with_fallback(
            pool,
            filter_prompt,
            None,
            "Triaje",
            on_status=lambda msg: ui.update_row(row_key, msg),
            file_paths=small_scanned or None,
            models_chain=TRIAGE_MODELS_CHAIN,
        )
        if m_name not in used_models:
            used_models.append(m_name)
        parsed = _extract_json_object(resp.text or "")
        if parsed and isinstance(parsed.get("relevant"), list):
            wanted = {str(x).strip().lower() for x in parsed["relevant"]}
            selected = [d for d in docs if d.name.lower() in wanted]

    # 2. Acotado literal por rangos de paginas en PDFs grandes (> LARGE_PDF_PAGES)
    slices_dir = pipe_dir / "slices"
    final_docs: list[Path] = []
    slice_notes: list[str] = []

    for d in selected:
        pgs = pages_map.get(d, [])
        if d.suffix.lower() != ".pdf" or len(pgs) <= LARGE_PDF_PAGES:
            final_docs.append(d)
            continue

        avg = (sum(len(p) for p in pgs) / len(pgs)) if pgs else 0
        if avg >= 40:
            outline = build_pdf_outline_digest(pgs, max_total_chars=100000)
            slice_prompt = (
                f'Este PDF "{d.name}" tiene {len(pgs)} paginas.\n'
                "Identifica los rangos de paginas (1-indexados) de los temas o apartados COMPLETOS que corresponden "
                "a esta transcripcion para extraerlos literalmente SIN resumir.\n"
                'Devuelve SOLO JSON valido: {"ranges": [[inicio, fin], ...]} (o {"ranges": []} si no guarda relacion).\n\n'
                f"TRANSCRIPCION (muestra):\n{tx_sample}\n\n"
                f"INDICE / PAGINAS DEL PDF:\n{outline}"
            )
            resp, m_name = generate_with_fallback(
                pool,
                slice_prompt,
                None,
                f"Acotando {d.name}",
                on_status=lambda msg: ui.update_row(row_key, msg),
                models_chain=TRIAGE_MODELS_CHAIN,
            )
            if m_name not in used_models:
                used_models.append(m_name)
            parsed = _extract_json_object(resp.text or "")
            if parsed is not None and isinstance(parsed.get("ranges"), list):
                ranges = parse_page_ranges(parsed["ranges"], len(pgs))
                if not ranges:
                    continue
                sliced_path = slices_dir / f"{d.stem}_acotado.pdf"
                slice_pdf_pages(d, ranges, sliced_path)
                kept_pages = sum(b - a + 1 for a, b in ranges)
                slice_notes.append(f"{kept_pages}/{len(pgs)} pags.")
                final_docs.append(sliced_path)
            else:
                final_docs.append(d)
        else:
            # PDF grande escaneado: evaluar bloques de paginas y conservar los relevantes
            parts = split_pdf_into_chunks(d, PDF_CHUNK_PAGES, slices_dir)
            kept_ranges: list[tuple[int, int]] = []
            for p_start, p_end, part_path in parts:
                chunk_prompt = (
                    f'Bloque de paginas {p_start}-{p_end} de "{d.name}". '
                    '¿Tiene relacion con esta transcripcion? Responde SOLO JSON: {"relevant": true} o {"relevant": false}.\n\n'
                    f"TRANSCRIPCION (muestra):\n{tx_sample}"
                )
                resp, m_name = generate_with_fallback(
                    pool,
                    chunk_prompt,
                    None,
                    f"Triaje {d.name}",
                    on_status=lambda msg: ui.update_row(row_key, msg),
                    file_paths=[part_path],
                    models_chain=TRIAGE_MODELS_CHAIN,
                )
                if m_name not in used_models:
                    used_models.append(m_name)
                parsed = _extract_json_object(resp.text or "")
                if parsed is None or parsed.get("relevant") is True:
                    kept_ranges.append((p_start, p_end))
            if kept_ranges:
                merged_ranges = parse_page_ranges([[a, b] for a, b in kept_ranges], len(pgs))
                sliced_path = slices_dir / f"{d.stem}_acotado.pdf"
                slice_pdf_pages(d, merged_ranges, sliced_path)
                kept_pages = sum(b - a + 1 for a, b in merged_ranges)
                slice_notes.append(f"{kept_pages}/{len(pgs)} pags.")
                final_docs.append(sliced_path)

    if slice_notes:
        info = f"{len(final_docs)}/{len(docs)} docs ({', '.join(slice_notes)})" if needs_filter else ", ".join(slice_notes)
    else:
        info = f"{len(final_docs)}/{len(docs)} docs"
    return final_docs, info, used_models


def plan_large_pdf_topics(
    pool: ClientPool,
    pdf_path: Path,
    transcripts: list[str],
    pipe_dir: Path,
    ui: PipelineUI,
    row_key: str = "triage",
) -> tuple[list[dict], str, list[str]]:
    """Divide un PDF grande por temas segun la transcripcion (1 PDF por tema o muestra de estilo si es tema nuevo)."""
    pages = inspect_pdf_pages(pdf_path)
    slices_dir = pipe_dir / "slices"
    slices_dir.mkdir(exist_ok=True)
    used_models: list[str] = []
    avg = (sum(len(p) for p in pages) / len(pages)) if pages else 0

    if avg >= 40:
        ui.update_row(row_key, "[accent]Dividiendo por temas...[/]", "")
        tx_sample = _sample_transcripts(transcripts)
        outline = build_pdf_outline_digest(pages, max_total_chars=100000)
        prompt = (
            f'Este PDF "{pdf_path.name}" es muy grande ({len(pages)} paginas).\n'
            "En lugar de generar un PDF aun mas grande, queremos trabajar POR TEMAS:\n"
            "- Si la transcripcion amplia o continua 1 o varios temas que ya estan en este PDF, "
            "devuelve cada tema por separado con sus rangos exactos de paginas (1-indexados, tema completo SIN resumir).\n"
            "- Si la transcripcion trata un TEMA NUEVO distinto a los del PDF, devuelve {\"topics\": []}.\n"
            'Formato JSON estricto: {"topics": [{"title": "Tema corto", "ranges": [[inicio, fin]]}]}\n\n'
            f"TRANSCRIPCION (muestra):\n{tx_sample}\n\n"
            f"INDICE / PAGINAS DEL PDF:\n{outline}"
        )
        resp, m_name = generate_with_fallback(
            pool,
            prompt,
            None,
            f"Dividiendo {pdf_path.name}",
            on_status=lambda msg: ui.update_row(row_key, msg),
            models_chain=TRIAGE_MODELS_CHAIN,
        )
        used_models.append(m_name)
        parsed = _extract_json_object(resp.text or "")
        raw_topics = parsed.get("topics") if (parsed and isinstance(parsed.get("topics"), list)) else []

        jobs: list[dict] = []
        notes: list[str] = []
        for idx, t in enumerate(raw_topics, 1):
            if not isinstance(t, dict):
                continue
            ranges = parse_page_ranges(t.get("ranges"), len(pages))
            if not ranges:
                continue
            sliced = slices_dir / f"{pdf_path.stem}_tema{idx}.pdf"
            slice_pdf_pages(pdf_path, ranges, sliced)
            kept = sum(b - a + 1 for a, b in ranges)
            title = str(t.get("title") or f"Tema {idx}").strip()
            jobs.append({"title": title, "path": sliced, "style_only": False, "pages": kept})
            notes.append(f"{kept} pags.")

        if jobs:
            return jobs, f"{len(jobs)} tema(s) ({', '.join(notes)})", used_models

        style_n = min(5, len(pages))
        style_path = slices_dir / f"{pdf_path.stem}_estilo.pdf"
        slice_pdf_pages(pdf_path, [(1, style_n)], style_path)
        return (
            [{"title": None, "path": style_path, "style_only": True, "pages": style_n}],
            f"nuevo tema (estilo {style_n} pags.)",
            used_models,
        )

    sel, info, s_models = select_and_slice_docs(pool, [pdf_path], transcripts, pipe_dir, ui, row_key=row_key)
    for m in s_models:
        if m not in used_models:
            used_models.append(m)
    if sel:
        return [{"title": None, "path": sel[0], "style_only": False, "pages": 0}], info, used_models

    style_n = min(5, len(pages))
    style_path = slices_dir / f"{pdf_path.stem}_estilo.pdf"
    if style_n > 0:
        slice_pdf_pages(pdf_path, [(1, style_n)], style_path)
    return (
        [{"title": None, "path": style_path, "style_only": True, "pages": style_n}],
        f"nuevo tema (estilo {style_n} pags.)",
        used_models,
    )


def _split_long_block(text: str, max_words: int) -> list[str]:
    """Divide un bloque de transcripcion largo en partes <= max_words sin perder ninguna palabra."""
    if len(text.split()) <= max_words:
        return [text]

    pieces: list[str] = []
    for para in text.split("\n\n"):
        if len(para.split()) <= max_words:
            pieces.append(para)
            continue
        for line in para.splitlines():
            words = line.split()
            if len(words) <= max_words:
                pieces.append(line)
            else:
                for i in range(0, len(words), max_words):
                    pieces.append(" ".join(words[i:i + max_words]))

    chunks: list[str] = []
    cur: list[str] = []
    cur_words = 0
    for piece in pieces:
        pw = len(piece.split())
        if cur and cur_words + pw > max_words:
            chunks.append("\n\n".join(cur))
            cur = [piece]
            cur_words = pw
        else:
            cur.append(piece)
            cur_words += pw
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks or [text]


def _partition_blocks_by_budget(blocks: list[str], indices: list[int], budget: int) -> list[list[int]]:
    groups: list[list[int]] = []
    cur: list[int] = []
    cur_w = 0
    for idx in indices:
        w = len(blocks[idx - 1].split())
        if cur and cur_w + w > budget:
            groups.append(cur)
            cur = [idx]
            cur_w = w
        else:
            cur.append(idx)
            cur_w += w
    if cur:
        groups.append(cur)
    return groups


def plan_transcript_topics(
    pool: ClientPool,
    transcripts: list[str],
    ui: PipelineUI,
    row_key: str = "triage",
    fallback_title: str = "clase",
    max_words_per_topic: int = MAX_TOPIC_WORDS,
) -> tuple[list[dict], str, list[str]]:
    """Agrupa bloques de transcripcion larga en varios PDFs por temas sin resumir ni omitir bloques."""
    ui.update_row(row_key, "[accent]Dividiendo por temas...[/]", "")
    blocks: list[str] = []
    sub_budget = max(4000, max_words_per_topic // 2)
    for t in transcripts:
        if len(t.split()) > max_words_per_topic:
            blocks.extend(_split_long_block(t, max_words=sub_budget))
        else:
            blocks.append(t)

    n_blocks = len(blocks)
    digest_lines: list[str] = []
    per_block_chars = max(240, min(900, 36000 // max(1, n_blocks)))
    for i, blk in enumerate(blocks, 1):
        w = len(blk.split())
        compact = re.sub(r"\s+", " ", blk).strip()
        if len(compact) <= per_block_chars:
            excerpt = compact
        else:
            part = max(80, per_block_chars // 3)
            mid = len(compact) // 2
            excerpt = f"{compact[:part]} [...] {compact[max(0, mid - part // 2):mid + part // 2]} [...] {compact[-part:]}"
        digest_lines.append(f"[BLOQUE {i} ({w} palabras)] {excerpt}")
    digest = "\n".join(digest_lines)[:36000]

    prompt = (
        f'Esta clase es muy extensa ({n_blocks} bloques).\n'
        "Para no perder ningun detalle ni resumir contenido, dividela en 2 o mas PDFs separados por temas coherentes "
        "(manteniendo cada PDF por debajo de unas 20.000 palabras).\n"
        f"REGLAS ESTRICTAS:\n"
        f"- Incluye TODOS los bloques del 1 al {n_blocks} exactamente una vez (sin omitir ni uno solo).\n"
        '- Formato JSON estricto: {"topics": [{"title": "Tema corto descriptivo", "blocks": [1, 2]}]}\n\n'
        f"RESUMEN DE BLOQUES:\n{digest}"
    )

    used_models: list[str] = []
    raw_topics: list = []
    try:
        resp, m_name = generate_with_fallback(
            pool,
            prompt,
            None,
            "Planificando temas",
            on_status=lambda msg: ui.update_row(row_key, msg),
            models_chain=TRIAGE_MODELS_CHAIN,
        )
        used_models.append(m_name)
        parsed = _extract_json_object(resp.text or "")
        if parsed and isinstance(parsed.get("topics"), list):
            raw_topics = parsed["topics"]
    except Exception:
        raw_topics = []

    topics: list[tuple[str, list[int]]] = []
    seen_global: set[int] = set()
    for idx, t in enumerate(raw_topics, 1):
        if not isinstance(t, dict):
            continue
        raw_b = t.get("blocks")
        if not isinstance(raw_b, list):
            continue
        valid_b: list[int] = []
        for b in raw_b:
            if isinstance(b, int) and 1 <= b <= n_blocks and b not in seen_global:
                seen_global.add(b)
                valid_b.append(b)
        if valid_b:
            title = str(t.get("title") or f"{fallback_title} — Tema {idx}").strip()
            topics.append((title, valid_b))

    missing = [b for b in range(1, n_blocks + 1) if b not in seen_global]
    if missing:
        if not topics:
            topics = [(fallback_title, list(range(1, n_blocks + 1)))]
        else:
            for m_idx in missing:
                best_pos = min(
                    range(len(topics)),
                    key=lambda ti: min(abs(x - m_idx) for x in topics[ti][1]),
                )
                topics[best_pos][1].append(m_idx)
                topics[best_pos][1].sort()

    if len(topics) < 2 and n_blocks >= 2:
        parts = _partition_blocks_by_budget(blocks, list(range(1, n_blocks + 1)), LARGE_TRANSCRIPT_WORDS)
        if len(parts) < 2 and n_blocks >= 2:
            mid_p = n_blocks // 2
            parts = [list(range(1, mid_p + 1)), list(range(mid_p + 1, n_blocks + 1))]
        topics = [(f"{fallback_title} — Parte {k}", grp) for k, grp in enumerate(parts, 1)]

    jobs: list[dict] = []
    for title, b_list in topics:
        tot_w = sum(len(blocks[b - 1].split()) for b in b_list)
        if tot_w > max_words_per_topic and len(b_list) > 1:
            sub_groups = _partition_blocks_by_budget(blocks, b_list, max_words_per_topic)
            for p_idx, grp in enumerate(sub_groups, 1):
                jobs.append({"title": f"{title} (Parte {p_idx})", "transcripts": [blocks[b - 1] for b in grp]})
        else:
            jobs.append({"title": title, "transcripts": [blocks[b - 1] for b in b_list]})

    return jobs, f"{len(jobs)} tema(s) por transcripcion", used_models


def _enforce_job_transcript_budget(
    topic_jobs: list[dict],
    all_transcripts: list[str],
    fallback_title: str,
    max_words: int = MAX_TOPIC_WORDS,
) -> list[dict]:
    """Garantiza que ningun job supere max_words sin resumir ni perder texto."""
    result: list[dict] = []
    for job in topic_jobs:
        txs = job.get("transcripts") or all_transcripts
        total_w = sum(len(t.split()) for t in txs)
        if total_w <= max_words:
            out_job = dict(job)
            out_job["transcripts"] = txs
            result.append(out_job)
            continue

        expanded: list[str] = []
        for t in txs:
            expanded.extend(_split_long_block(t, max_words=max_words))
        groups = _partition_blocks_by_budget(expanded, list(range(1, len(expanded) + 1)), max_words)
        base_title = job.get("title") or fallback_title
        for k, grp in enumerate(groups, 1):
            out_job = dict(job)
            out_job["title"] = f"{base_title} (Parte {k})" if len(groups) > 1 else base_title
            out_job["transcripts"] = [expanded[idx - 1] for idx in grp]
            result.append(out_job)
    return result


def get_duration(path: Path) -> float:
    res = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(res.stdout.strip())


def _last_float(pattern: str, text: str) -> float | None:
    matches = re.findall(pattern, text)
    if not matches:
        return None
    try:
        val = float(matches[-1])
        return val if math.isfinite(val) else None
    except ValueError:
        return None


def probe_audio_stats(path: Path, low_prio: int = 0, in_dur: float | None = None) -> tuple[float, float, float] | None:
    """Mide sonoridad integrada (LUFS), rango dinamico (LRA) y suelo bajo en 60s."""
    cmd = ["ffmpeg"]
    if in_dur is not None and in_dur > 120:
        cmd += ["-ss", "30"]
    cmd += [
        "-t", "60", "-i", str(path),
        "-af", "highpass=f=85,lowpass=f=7800,ebur128=framelog=quiet",
        "-f", "null", "-",
    ]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=low_prio,
        )
    except Exception:
        return None
    txt = res.stderr
    i_lufs = _last_float(r"I:\s*([-+\d.]+)\s*LUFS", txt)
    lra = _last_float(r"LRA:\s*([-+\d.]+)\s*LU", txt)
    lra_low = _last_float(r"LRA low:\s*([-+\d.]+)\s*LUFS", txt)
    if i_lufs is None or lra is None:
        return None
    if lra_low is None:
        lra_low = i_lufs - (lra * 0.5)
    return i_lufs, lra, lra_low


def build_adaptive_filter(audio: Path, low_prio: int = 0, in_dur: float | None = None) -> str:
    """Calcula pre-ganancia por LUFS, limitador anti-tecleo/golpes y afftdn adaptado al audio."""
    stats = probe_audio_stats(audio, low_prio, in_dur=in_dur)
    if stats is None:
        return FFMPEG_FILTER
    i_lufs, lra, lra_low = stats
    pre_gain = max(-18.0, min(24.0, -32.0 - i_lufs))
    lra_low_post = lra_low + pre_gain

    if lra >= 18.0:
        nr = 10
        nf = max(-50.0, min(-30.0, lra_low_post + 4.0))
    elif lra >= 12.0:
        nr = 18
        nf = max(-45.0, min(-25.0, lra_low_post + 10.0))
    else:
        nr = 20
        nf = max(-45.0, min(-24.0, lra_low_post + 12.0))

    return (
        f"highpass=f=85,lowpass=f=7800,"
        f"volume={pre_gain:.1f}dB,"
        f"adeclick=t=2:w=30,"
        f"alimiter=limit=0.14:level=0:attack=0.5:release=25,"
        f"afftdn=nr={nr}:nf={nf:.0f}:tn=1,"
        f"equalizer=f=300:t=q:w=1.0:g=-3,"
        f"equalizer=f=2800:t=q:w=1.2:g=3,"
        f"dynaudnorm=f=200:g=21:p=0.9:m=10,"
        f"loudnorm=I=-16:TP=-1.5:LRA=11"
    )


def _run_ffmpeg_with_progress(
    audio: Path,
    af_filter: str,
    clean_mp3: Path,
    in_dur: float,
    low_prio: int,
    on_pct,
):
    cmd = [
        "ffmpeg", "-y", "-i", str(audio),
        "-af", af_filter,
        "-c:a", "libmp3lame", "-b:a", "128k",
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        str(clean_mp3),
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=low_prio,
    )
    err_chunks: list[str] = []
    def _drain_stderr():
        if proc.stderr is not None:
            err_chunks.append(proc.stderr.read())
    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()
    last_pct = -3
    if proc.stdout is not None:
        for line in proc.stdout:
            s = line.strip()
            if s.startswith("out_time_us="):
                try:
                    us = int(s.split("=", 1)[1])
                    pct = min(99, max(0, int((us / 1_000_000.0) / max(in_dur, 0.1) * 100)))
                    if pct - last_pct >= 3:
                        last_pct = pct
                        on_pct(pct)
                except ValueError:
                    pass
    err_thread.join(timeout=5)
    stderr_out = "".join(err_chunks)
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"Error procesando {audio.name}: {stderr_out.strip()}")


def _file_fingerprint(path: Path) -> str:
    st = path.stat()
    raw = f"{path.name}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:10]


def _process_one_audio(
    idx: int,
    audio: Path,
    clean_dir: Path,
    chunks_dir: Path,
    ui: PipelineUI,
) -> tuple[list[Path], float]:
    row_key = f"audio_{idx}"
    low_prio = subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0
    fp = _file_fingerprint(audio)
    clean_mp3 = clean_dir / f"{fp}_{audio.stem}_limpio.mp3"

    if not clean_mp3.exists():
        ui.update_row(row_key, "[accent]Procesando 0%[/]", "")
        in_dur = get_duration(audio)
        af_filter = build_adaptive_filter(audio, low_prio, in_dur=in_dur)
        clean_tmp = clean_mp3.with_suffix(".tmp.mp3")
        try:
            _run_ffmpeg_with_progress(
                audio,
                af_filter,
                clean_tmp,
                in_dur,
                low_prio,
                lambda p: ui.update_row(row_key, f"[accent]Procesando {p}%[/]", ""),
            )
            os.replace(clean_tmp, clean_mp3)
        except Exception:
            clean_tmp.unlink(missing_ok=True)
            raise

    dur = get_duration(clean_mp3)
    if dur <= CHUNK_SECONDS + 120:
        ui.update_row(row_key, "[ok]✓ Listo[/]", fmt_duration(dur))
        return [clean_mp3], dur

    chunks: list[Path] = []
    start = 0.0
    part = 1
    step = CHUNK_SECONDS - OVERLAP_SECONDS
    while start < dur:
        chunk_path = chunks_dir / f"{fp}_{audio.stem}_parte{part:02d}.mp3"
        if not chunk_path.exists():
            ui.update_row(row_key, "[accent]Procesando...[/]", f"parte {part}")
            chunk_tmp = chunk_path.with_suffix(".tmp.mp3")
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-ss", str(start), "-t", str(CHUNK_SECONDS),
                        "-i", str(clean_mp3), "-c", "copy", str(chunk_tmp), "-loglevel", "error",
                    ],
                    check=True,
                    creationflags=low_prio,
                )
                os.replace(chunk_tmp, chunk_path)
            except Exception:
                chunk_tmp.unlink(missing_ok=True)
                raise
        chunks.append(chunk_path)
        if start + CHUNK_SECONDS >= dur:
            break
        start += step
        part += 1

    ui.update_row(row_key, "[ok]✓ Listo[/]", f"{fmt_duration(dur)} ({len(chunks)} partes)")
    return chunks, dur


def clean_and_split_audios(
    audios: list[Path],
    pipe_dir: Path,
    max_workers: int,
    ui: PipelineUI,
    step_label: str = "Procesando",
) -> tuple[list[Path], float]:
    clean_dir = pipe_dir / "clean"
    chunks_dir = pipe_dir / "chunks"
    clean_dir.mkdir(exist_ok=True)
    chunks_dir.mkdir(exist_ok=True)

    noun = "audio..." if len(audios) == 1 else f"{len(audios)} audios..."
    ui.set_progress(description=f"{step_label} · Procesando {noun}", total=len(audios), completed=0)
    results: list[tuple[list[Path], float] | None] = [None] * len(audios)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_process_one_audio, idx, audio, clean_dir, chunks_dir, ui): idx - 1
            for idx, audio in enumerate(audios, 1)
        }
        for fut, pos in futures.items():
            results[pos] = fut.result()
            ui.set_progress(advance=1)
    all_chunks = [c for item in results if item for c in item[0]]
    total_dur = sum(item[1] for item in results if item)
    return all_chunks, total_dur


def _clean_repetition_loops(text: str) -> tuple[str, int]:
    """Elimina bucles de repeticion de palabras, frases o lineas generados ante silencios."""
    orig_words = len(text.split())
    if not text.strip():
        return text, 0

    lines = text.splitlines()
    norm_lines = [re.sub(r"\s+", " ", ln.strip().lower()) for ln in lines]
    is_divider = [bool(re.match(r"^[\|\s:\-]+$", ln.strip())) for ln in lines]

    # Paso 1a: colapsar lineas identicas consecutivas (>= 3 veces)
    dedup_lines: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        nl = norm_lines[i]
        if nl and not is_divider[i]:
            j = i + 1
            while j < n and norm_lines[j] == nl:
                j += 1
            if j - i >= 3:
                dedup_lines.append(lines[i])
                i = j
                continue
        dedup_lines.append(lines[i])
        i += 1

    # Paso 1b: colapsar bloques repetidos de 2..6 lineas consecutivas (>= 3 veces)
    for blk_sz in range(2, 7):
        if len(dedup_lines) < blk_sz * 3:
            continue
        nl_cur = [re.sub(r"\s+", " ", ln.strip().lower()) for ln in dedup_lines]
        next_lines: list[str] = []
        i = 0
        m = len(dedup_lines)
        while i < m:
            if i + blk_sz * 3 <= m:
                pat = nl_cur[i:i + blk_sz]
                if any(p and not re.match(r"^[\|\s:\-]+$", p) for p in pat):
                    reps = 1
                    while i + (reps + 1) * blk_sz <= m and nl_cur[i + reps * blk_sz:i + (reps + 1) * blk_sz] == pat:
                        reps += 1
                    if reps >= 3:
                        next_lines.extend(dedup_lines[i:i + blk_sz])
                        i += reps * blk_sz
                        continue
            next_lines.append(dedup_lines[i])
            i += 1
        dedup_lines = next_lines

    # Paso 2: colapsar bucles de palabras/frases dentro de lineas (>= 6 palabras)
    final_lines: list[str] = []
    for ln in dedup_lines:
        words = ln.split()
        if len(words) < 6:
            final_lines.append(ln)
            continue
        norm = [w.strip(".,;:!?¡¿\"'()[]{}").lower() for w in words]
        out_w: list[str] = []
        wi = 0
        wn = len(words)
        while wi < wn:
            matched = False
            max_k = min(30, (wn - wi) // 3)
            for k in range(1, max_k + 1):
                if norm[wi] != norm[wi + k]:
                    continue
                pat = norm[wi:wi + k]
                if not any(pat):
                    continue
                reps = 1
                while wi + (reps + 1) * k <= wn and norm[wi + reps * k:wi + (reps + 1) * k] == pat:
                    reps += 1
                thresh = 6 if k == 1 else (4 if k <= 4 else 3)
                if reps >= thresh:
                    out_w.extend(words[wi:wi + k])
                    wi += reps * k
                    matched = True
                    break
            if not matched:
                out_w.append(words[wi])
                wi += 1
        final_lines.append(" ".join(out_w))

    cleaned = "\n".join(final_lines).strip()
    clean_words = len(cleaned.split())
    return cleaned, max(0, orig_words - clean_words)


def _is_suspicious_loop(orig_words: int, dropped_words: int) -> bool:
    return dropped_words >= LOOP_RETRY_DROPPED_WORDS or orig_words > MAX_CHUNK_WORDS_SANE


def transcribe_chunks(
    pool: ClientPool,
    chunks: list[Path],
    vocab: list[str],
    pipe_dir: Path,
    ui: PipelineUI,
    step_label: str = "Transcribiendo",
) -> tuple[list[str], list[str]]:
    _, types = _load_genai()
    tx_dir = pipe_dir / "transcripts"
    tx_dir.mkdir(exist_ok=True)
    transcripts: list[str] = []
    used_models: list[str] = []
    vocab_note = f" Vocabulario tecnico de referencia: {', '.join(vocab[:100])}." if vocab else ""

    for i in range(1, len(chunks) + 1):
        lbl = "Transcripcion" if len(chunks) == 1 else f"Transcripcion ({i}/{len(chunks)})"
        ui.add_row(f"tx_{i}", lbl)
    ui.set_progress(description=f"{step_label} · Transcribiendo...", total=len(chunks), completed=0)

    def _upload_and_wait(path: Path, on_status=None):
        up_idx = pool.current_idx
        c = pool.get_client(up_idx)
        f = _upload_local_file(c, path, on_status=on_status)
        return up_idx, wait_file_active(c, f, on_status=on_status)

    def _delete_remote(item):
        if item is not None:
            up_idx, f = item
            try:
                pool.get_client(up_idx).files.delete(name=f.name)
            except Exception:
                pass

    def _transcribe_once(chunk_path: Path, up_item, row_key: str, idx: int, retry_anti_loop: bool = False):
        up_idx, uploaded = up_item
        prompt_txt = (
            "Transcribe integramente y de forma literal este audio de clase en espanol, "
            "manteniendo todos los detalles tecnicos sin resumir. "
            f"No repitas palabras ni frases en bucle ante silencios o ruido; omite los silencios.{vocab_note}"
        )
        if retry_anti_loop:
            prompt_txt += (
                " IMPORTANTE: El intento anterior entro en bucle de repeticion. "
                "Transcribe cada frase una sola vez y salta cualquier silencio o ruido de fondo."
            )
        cfg_kwargs: dict = {
            "thinking_config": types.ThinkingConfig(thinking_level="LOW"),
            "max_output_tokens": TRANSCRIBE_MAX_OUTPUT_TOKENS,
        }
        if retry_anti_loop:
            cfg_kwargs["temperature"] = 0.2
        resp, m_used = generate_with_fallback(
            pool,
            [prompt_txt],
            types.GenerateContentConfig(**cfg_kwargs),
            f"Transcripcion {idx}/{len(chunks)}",
            on_status=lambda msg, rk=row_key: ui.update_row(rk, msg),
            file_paths=[chunk_path],
            preuploaded=(up_idx, [uploaded]),
        )
        return (resp.text or "").strip(), m_used

    def _pick_better_text(clean_a: str, orig_a_w: int, drop_a: int, raw_b: str) -> str:
        clean_b, drop_b = _clean_repetition_loops(raw_b)
        orig_b_w = len(raw_b.split())
        if clean_b and (
            not _is_suspicious_loop(orig_b_w, drop_b)
            or drop_b < drop_a
            or len(clean_b.split()) > len(clean_a.split())
        ):
            return clean_b
        return clean_a

    def _save_tx(tx_file: Path, content: str):
        tx_tmp = tx_file.with_suffix(".tmp.txt")
        tx_tmp.write_text(content, encoding="utf-8")
        os.replace(tx_tmp, tx_file)

    prefetch_fut = None
    with ThreadPoolExecutor(max_workers=1) as up_pool:
        try:
            for i, chunk in enumerate(chunks, 1):
                row_key = f"tx_{i}"
                tx_file = tx_dir / f"{chunk.stem}.txt"
                if tx_file.exists():
                    raw_cached = tx_file.read_text(encoding="utf-8")
                    orig_w = len(raw_cached.split())
                    clean_cached, dropped = _clean_repetition_loops(raw_cached)
                    if _is_suspicious_loop(orig_w, dropped):
                        ui.update_row(row_key, "[warn]Bucle en cache, reintentando...[/]", "")
                        retry_item = _upload_and_wait(chunk, on_status=lambda msg, rk=row_key: ui.update_row(rk, msg))
                        try:
                            retry_raw, retry_model = _transcribe_once(chunk, retry_item, row_key, i, retry_anti_loop=True)
                            text = _pick_better_text(clean_cached, orig_w, dropped, retry_raw)
                            used_models.append(retry_model)
                        finally:
                            _delete_remote(retry_item)
                        _save_tx(tx_file, text)
                    else:
                        text = clean_cached
                        if dropped > 0:
                            _save_tx(tx_file, text)

                    words = len(text.split())
                    ui.update_row(row_key, "[ok]✓ Listo[/]", f"{words:,} palabras")
                    transcripts.append(text)
                    ui.set_progress(advance=1)
                    if prefetch_fut is not None:
                        try:
                            _delete_remote(prefetch_fut.result())
                        except Exception:
                            pass
                        prefetch_fut = None
                    continue

                ui.update_row(row_key, "[accent]Transcribiendo...[/]", "")
                up_item = None
                if prefetch_fut is not None:
                    try:
                        cand = prefetch_fut.result()
                        if cand[0] == pool.current_idx:
                            up_item = cand
                        else:
                            _delete_remote(cand)
                    except Exception:
                        pass
                    prefetch_fut = None
                if up_item is None:
                    up_item = _upload_and_wait(chunk, on_status=lambda msg, rk=row_key: ui.update_row(rk, msg))

                if i < len(chunks):
                    next_chunk = chunks[i]
                    next_tx = tx_dir / f"{next_chunk.stem}.txt"
                    if not next_tx.exists():
                        prefetch_fut = up_pool.submit(_upload_and_wait, next_chunk)

                try:
                    raw_text, model_used = _transcribe_once(chunk, up_item, row_key, i, retry_anti_loop=False)
                    orig_w = len(raw_text.split())
                    clean_text, dropped = _clean_repetition_loops(raw_text)
                    if _is_suspicious_loop(orig_w, dropped):
                        ui.update_row(row_key, "[warn]Bucle detectado, reintentando...[/]", "")
                        retry_raw, retry_model = _transcribe_once(chunk, up_item, row_key, i, retry_anti_loop=True)
                        text = _pick_better_text(clean_text, orig_w, dropped, retry_raw)
                        model_used = retry_model
                    else:
                        text = clean_text
                    _save_tx(tx_file, text)
                    if not text.strip():
                        raise RuntimeError(f"Transcripcion vacia en bloque {i}/{len(chunks)}.")
                    transcripts.append(text)
                    used_models.append(model_used)
                    words = len(text.split())
                    ui.update_row(row_key, "[ok]✓ Listo[/]", f"{words:,} palabras")
                finally:
                    _delete_remote(up_item)
                ui.set_progress(advance=1)
        finally:
            if prefetch_fut is not None:
                try:
                    _delete_remote(prefetch_fut.result(timeout=2))
                except Exception:
                    pass

    return transcripts, used_models


def strip_markdown_fences(md_text: str) -> str:
    text = md_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def synthesize_notes(
    pool: ClientPool,
    transcripts: list[str],
    vocab: list[str],
    title_hint: str,
    ui: PipelineUI,
    base_notes: list[Path] | None = None,
    mode: str = "transcripcion",
    guide_docs: list[Path] | None = None,
    style_only: bool = False,
    topic_focus: str | None = None,
) -> tuple[str, str]:
    ui.update_row("review", "[accent]Revisando...[/]", "")
    joined = "\n\n".join(
        f"=== BLOQUE {i} ===\n{t}" for i, t in enumerate(transcripts, 1)
    )
    vocab_hint = ", ".join(vocab) if vocab else "N/A"
    focus_section = (
        f'\nENFOQUE DE TEMA: Centra este PDF exclusivamente en "{topic_focus}" '
        "(los demas temas se generan en PDFs separados).\n"
        if topic_focus
        else ""
    )

    upload_paths: list[Path] = []
    docx_texts: list[str] = []
    base_section = ""

    if style_only:
        upload_paths.extend(base_notes or [])
        if mode == "apuntes":
            for g in (guide_docs or []):
                if g.suffix.lower() == ".docx":
                    txt = read_docx_text(g, max_chars=12000)
                    if txt:
                        docx_texts.append(f"=== MUESTRA DE ESTILO: {g.name} ===\n{txt}")
                else:
                    upload_paths.append(g)
        mode_section = (
            "\nMODO REFERENCIA DE ESTILO SOLAMENTE:\n"
            "El fragmento PDF adjunto pertenece a un documento grande previo y se envia UNICAMENTE como muestra de estilo, estructura, formato y tono. "
            "NO copies ni repitas su contenido; redacta un documento independiente SOLO con el tema nuevo de la transcripcion siguiendo exactamente esa misma linea.\n"
        )
        style_rule = (
            "3. Redacta en prosa academica clara y estructurada para estudiar (usa listas, tablas o negritas cuando ayuden), "
            "atribuyendo opiniones o ejemplos al docente solo cuando aporte valor."
        )
    else:
        if base_notes:
            names = ", ".join(p.name for p in base_notes)
            base_section = (
                f"\nAPUNTES BASE ADJUNTOS ({names}):\n"
                "Se adjuntan uno o mas PDFs de apuntes/transcripciones previas de esta misma clase/asignatura. "
                "USALOS COMO BASE INTEGRA: conserva TODO su contenido, explicaciones, estructura y notas `(nota: ...)` sin resumir ni omitir nada, "
                "e integra los nuevos bloques en su posicion cronologica y tematica coherente "
                "(al principio, en medio o al final segun el hilo real del discurso), eliminando solo posibles duplicados o solapamientos.\n"
            )
            upload_paths.extend(base_notes)

        if mode == "apuntes":
            guides = list(guide_docs or [])
            for g in guides:
                if g.suffix.lower() == ".docx":
                    txt = read_docx_text(g)
                    if txt:
                        docx_texts.append(f"=== DOCUMENTO GUIA: {g.name} ===\n{txt}")
                else:
                    upload_paths.append(g)

            if len(guides) == 1:
                g_name = guides[0].name
                mode_section = (
                    f"\nMODO APUNTES — FUSION CON DOCUMENTO GUIA ({g_name}):\n"
                    "Se adjunta UN documento guia junto con la transcripcion de la clase. "
                    "FUSIONA ambos materiales en un unico documento de apuntes completos para estudiar: "
                    "conserva toda la estructura, definiciones y contenido integro del documento guia e integra en cada apartado correspondiente "
                    "las explicaciones, ejemplos, matices, casos y aclaraciones que dio el docente en clase (mas notas `(nota: ...)` cuando aporten contexto). "
                    "No resumas ni omitas informacion util ni del documento ni de la clase.\n"
                )
            elif len(guides) > 1:
                g_names = ", ".join(g.name for g in guides)
                mode_section = (
                    f"\nMODO APUNTES — MISMA LINEA QUE DOCUMENTOS GUIA ({g_names}):\n"
                    "Se adjuntan varios documentos de referencia de la asignatura. "
                    "Redacta los apuntes completos de esta clase SIGUIENDO LA MISMA LINEA, estructura, nivel de detalle, tono academico y terminologia de esos documentos, "
                    "sin copiar ni repetir temas que no pertenezcan a esta clase, y sin resumir ni perder las explicaciones del docente.\n"
                )
            else:
                mode_section = (
                    "\nMODO APUNTES — REDACCION PARA ESTUDIO:\n"
                    "Transforma la transcripcion en apuntes redactados, claros y listos para estudiar: "
                    "organiza por temas y subtemas, limpia muletillas y dialogo crudo pasando a prosa academica precisa, "
                    "conservando todas las explicaciones, ejemplos, matices y casos del docente (mas `(nota: ...)` cuando falte contexto o haya un posible desliz).\n"
                )
            style_rule = (
                "3. Redacta en prosa academica clara y estructurada para estudiar (usa listas, tablas o negritas cuando ayuden), "
                "atribuyendo opiniones o ejemplos al docente solo cuando aporte valor."
            )
        else:
            mode_section = (
                "\nMODO TRANSCRIPCION COMPLETA:\n"
                "Genera una transcripcion ordenada, limpia y fiel de lo dicho en clase, sin resumir ni transformar en apuntes sinteticos.\n"
            )
            style_rule = (
                "3. Identifica hablantes cuando se distinga (ej. **Profesora:**, **Profesor:**, **Alumno:**) sin inventar nombres que no salgan."
            )

    prompt = f"""Eres un editor academico experto. Tienes varios bloques de transcripcion de una clase sobre "{title_hint}".
ATENCION: Los audios originales pueden NO estar en orden cronologico (los nombres de archivo pueden venir desordenados) y pueden tener solapamientos de ~15s si proceden de un mismo audio dividido.
Vocabulario tecnico de referencia: {vocab_hint}
{focus_section}{base_section}{mode_section}
Instrucciones estrictas:
1. ORDENACION LOGICA Y CRONOLOGICA (PRIORITARIO): Antes de redactar, analiza el hilo del discurso (introduccion, referencias como "como deciamos antes", continuidad de frases cortadas, despedida/cierre) para detectar el orden real de los bloques (y su encaje con los apuntes base si los hay) aunque vengan desordenados. Reordenalos en su secuencia cronologica y tematica coherente, y elimina repeticiones de solapamientos sin perder contenido.
2. Estructura en Markdown claro: empieza en la primera linea con un titulo `# <Asignatura o Tema breve>` (ej. `# Palinologia Forense: Polen y Esporas`), luego secciones `##` y subtemas `###`, parrafos legibles y listas cuando ayuden.
{style_rule}
4. Corrige directamente SOLO erratas ortograficas o de oido obvias cuando el vocabulario de referencia y el contexto den certeza total. Si una posible errata de transcripcion NO es segura, deja el texto tal cual y añade `(probable error de transcripcion por «...»)`.
5. APUNTES, CONTEXTO Y CORRECCIONES AL DOCENTE `(nota: ...)`: No resumas ni borres las explicaciones del docente. Ademas:
   - Si se menciona de pasada un concepto, termino tecnico, autor, obra, caso o proceso sin explicarlo apenas, añade justo despues un apunte breve y util con formato `(nota: ...)` aportando la definicion o contexto necesario para estudiar.
   - Si detectas una frase cortada cuyo sentido se intuye, o un posible desliz/imprecision academica del docente (aunque no sea certeza absoluta), añade la aclaracion entre parentesis con `(nota: ...)` sin alterar lo que dijo.
6. Devuelve SOLO el documento Markdown final, sin comentarios meta previos ni posteriores.

NUEVA TRANSCRIPCION:
{joined}
"""
    _, types = _load_genai()
    call_contents = [*docx_texts, prompt] if docx_texts else prompt
    resp, used_model = generate_with_fallback(
        pool,
        call_contents,
        types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level="MEDIUM")
        ),
        "Revision",
        on_status=lambda msg: ui.update_row("review", msg),
        file_paths=upload_paths or None,
    )
    md_text = strip_markdown_fences(resp.text or "")
    if not md_text.strip():
        raise RuntimeError("Respuesta vacia del modelo al generar el documento.")
    ui.update_row("review", "[ok]✓ Listo[/]", "")
    return md_text, used_model


def _highlight_notes(text: str) -> str:
    out: list[str] = []
    i = 0
    lower = text.lower()
    markers = ("(nota:", "(probable error de transcripcion")
    n = len(text)
    while i < n:
        hits = [lower.find(m, i) for m in markers]
        hits = [h for h in hits if h != -1]
        if not hits:
            out.append(text[i:])
            break
        start = min(hits)
        out.append(text[i:start])
        depth = 0
        j = start
        while j < n:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        out.append(f'<span class="note">{text[start:j]}</span>')
        i = j
    return "".join(out)


def format_inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r'<span class="note">\1</span>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
    text = _highlight_notes(text)
    return text


def _render_table(rows: list[str]) -> str:
    parsed = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
    if len(parsed) >= 2 and all(re.fullmatch(r":?-{2,}:?", c) for c in parsed[1]):
        header, body = parsed[0], parsed[2:]
    else:
        header, body = parsed[0], parsed[1:]
    parts = ["<table><thead><tr>"]
    parts.extend(f"<th>{format_inline(c)}</th>" for c in header)
    parts.append("</tr></thead><tbody>")
    for row in body:
        parts.append("<tr>" + "".join(f"<td>{format_inline(c)}</td>" for c in row) + "</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def md_to_html(md_text: str) -> str:
    out: list[str] = []
    list_tag: str | None = None
    table_rows: list[str] = []
    in_code = False
    code_lines: list[str] = []

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def flush_table():
        nonlocal table_rows
        if table_rows:
            out.append(_render_table(table_rows))
            table_rows = []

    for raw_line in md_text.splitlines():
        s = raw_line.strip()
        if s.startswith("```"):
            flush_table()
            close_list()
            if in_code:
                out.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(raw_line)
            continue
        if s.startswith("|") and s.endswith("|"):
            close_list()
            table_rows.append(s)
            continue
        flush_table()
        if not s:
            close_list()
            continue
        if s == "---":
            close_list()
            out.append("<hr>")
        elif s.startswith("#### "):
            close_list()
            out.append(f"<h4>{format_inline(s[5:])}</h4>")
        elif s.startswith("### "):
            close_list()
            out.append(f"<h3>{format_inline(s[4:])}</h3>")
        elif s.startswith("## "):
            close_list()
            out.append(f"<h2>{format_inline(s[3:])}</h2>")
        elif s.startswith("# "):
            close_list()
            out.append(f"<h1>{format_inline(s[2:])}</h1>")
        elif s.startswith("> "):
            close_list()
            out.append(f"<blockquote>{format_inline(s[2:])}</blockquote>")
        elif s.startswith(("* ", "- ")):
            if list_tag != "ul":
                close_list()
                out.append("<ul>")
                list_tag = "ul"
            out.append(f"<li>{format_inline(s[2:])}</li>")
        elif m := re.match(r"^(\d+)\.\s+(.*)$", s):
            if list_tag != "ol":
                close_list()
                out.append("<ol>")
                list_tag = "ol"
            out.append(f"<li>{format_inline(m.group(2))}</li>")
        else:
            close_list()
            out.append(f"<p>{format_inline(s)}</p>")
    if code_lines:
        out.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
    flush_table()
    close_list()
    return "\n".join(out)


def find_browser() -> str:
    local_app = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        str(Path(local_app) / "Google" / "Chrome" / "Application" / "chrome.exe") if local_app else "",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        str(Path(local_app) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe") if local_app else "",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    for name in ("msedge", "chrome", "brave"):
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError("No se encontro Edge ni Chrome para exportar a PDF.")


def _export_pdf_fpdf2(body_html: str, md_text: str, pdf_path: Path):
    """Fallback puro Python con fpdf2 cuando Edge/Chrome no estan disponibles."""
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    reg = fonts_dir / "segoeui.ttf"
    if not reg.exists():
        reg = fonts_dir / "arial.ttf"
    if reg.exists():
        family = "SysFont"
        pdf.add_font(family, "", str(reg))
        bold = fonts_dir / ("segoeuib.ttf" if reg.name == "segoeui.ttf" else "arialbd.ttf")
        ital = fonts_dir / ("segoeuii.ttf" if reg.name == "segoeui.ttf" else "ariali.ttf")
        pdf.add_font(family, "B", str(bold if bold.exists() else reg))
        pdf.add_font(family, "I", str(ital if ital.exists() else reg))
        pdf.set_font(family, size=10.5)
        unicode_ok = True
    else:
        pdf.set_font("Helvetica", size=10.5)
        unicode_ok = False

    safe_html = re.sub(r"<span[^>]*>(.*?)</span>", r"<i>\1</i>", body_html, flags=re.DOTALL)
    if not unicode_ok:
        safe_html = safe_html.encode("latin-1", "replace").decode("latin-1")
    try:
        pdf.write_html(safe_html)
    except Exception:
        txt = md_text if unicode_ok else md_text.encode("latin-1", "replace").decode("latin-1")
        pdf.multi_cell(0, 6, txt)

    tmp_pdf = pdf_path.with_suffix(".tmp.pdf")
    try:
        pdf.output(str(tmp_pdf))
        tmp_pdf.replace(pdf_path)
    finally:
        tmp_pdf.unlink(missing_ok=True)


def export_pdf(md_text: str, pipe_dir: Path, pdf_path: Path):
    html_path = pipe_dir / "doc.html"
    body = md_to_html(md_text)
    doc = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<style>
  @page {{ size: A4; margin: 18mm 18mm 20mm 18mm; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; font-size: 10.5pt; line-height: 1.52; color: #1e293b; }}
  h1 {{ font-size: 18pt; color: #0f172a; border-bottom: 2px solid #0f172a; padding-bottom: 6px; margin-top: 0; }}
  h2 {{ font-size: 13.5pt; color: #1e3a8a; margin-top: 22px; margin-bottom: 8px; page-break-after: avoid; }}
  h3 {{ font-size: 11.5pt; color: #334155; margin-top: 16px; margin-bottom: 6px; page-break-after: avoid; }}
  h4 {{ font-size: 10.5pt; color: #475569; margin-top: 12px; margin-bottom: 4px; page-break-after: avoid; }}
  p {{ margin: 0 0 8px 0; text-align: justify; }}
  ul, ol {{ margin: 4px 0 10px 0; padding-left: 20px; }}
  li {{ margin-bottom: 4px; }}
  blockquote {{ margin: 8px 0; padding: 6px 12px; border-left: 3px solid #94a3b8; color: #475569; background: #f8fafc; }}
  table {{ width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 9.8pt; }}
  th, td {{ border: 1px solid #cbd5e1; padding: 5px 8px; text-align: left; }}
  th {{ background: #f1f5f9; font-weight: 600; }}
  hr {{ border: none; border-top: 1px solid #cbd5e1; margin: 18px 0; }}
  pre {{ background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 4px; padding: 8px 10px; font-family: Consolas, 'Courier New', monospace; font-size: 9.2pt; overflow-x: auto; white-space: pre-wrap; }}
  pre code {{ background: none; padding: 0; }}
  .note {{ color: #475569; font-style: italic; font-size: 9.8pt; background: #f1f5f9; padding: 1px 4px; border-radius: 3px; }}
</style>
</head>
<body>{body}</body>
</html>"""
    html_path.write_text(doc, encoding="utf-8")
    browser_err: Exception | None = None
    tmp_pdf = pdf_path.with_suffix(".tmp.pdf")
    try:
        browser = find_browser()
        res = subprocess.run(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--print-to-pdf={tmp_pdf}",
                html_path.as_uri(),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if res.returncode == 0 and tmp_pdf.exists() and tmp_pdf.stat().st_size > 200:
            tmp_pdf.replace(pdf_path)
            return
        browser_err = RuntimeError(f"Error exportando PDF ({res.returncode}): {res.stderr.strip()}")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        browser_err = exc
    finally:
        tmp_pdf.unlink(missing_ok=True)

    try:
        _export_pdf_fpdf2(body, md_text, pdf_path)
    except Exception:
        if browser_err is not None:
            raise browser_err
        raise


def _sanitize_slug(raw: str) -> str:
    title = re.sub(r"[*`]", "", raw).strip()
    title = re.sub(r'[\\/:*?"<>|]+', "", title)
    return re.sub(r"\s+", "_", title).strip("._")[:60].rstrip("._")


def derive_subject_slug(md_text: str, fallback: str) -> str:
    for line in md_text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            slug = _sanitize_slug(s[2:])
            if slug:
                return slug
    return _sanitize_slug(fallback) or "clase"


def _force_remove_readonly(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def cleanup_intermediate(pipe_dir: Path):
    """Elimina archivos temporales tras generar el PDF y borra .clase_pipeline si queda vacia (venv/api_key viven en %LOCALAPPDATA%\\Clase2PDF)."""
    for attempt in range(4):
        for sub in ("clean", "chunks", "transcripts", "slices", "topics"):
            p = pipe_dir / sub
            if p.exists():
                shutil.rmtree(p, onerror=_force_remove_readonly)
        for fname in ("doc.html", "vocab_cache.json", "Thumbs.db", "desktop.ini"):
            f = pipe_dir / fname
            if f.exists():
                try:
                    os.chmod(f, stat.S_IWRITE)
                    f.unlink()
                except OSError:
                    pass
        try:
            if pipe_dir.exists() and not any(pipe_dir.iterdir()):
                pipe_dir.rmdir()
                return
        except OSError:
            pass
        if not pipe_dir.exists():
            return
        if attempt < 3:
            time.sleep(0.25)


def unique_output_path(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_stem(f"{path.stem}_{n}")
        if not cand.exists():
            return cand
        n += 1


class _MockStat:
    def __init__(self, size: int):
        self.st_size = size


class _MockFile:
    def __init__(self, name: str, parent: Path, size: int):
        self.name = name
        self.parent = parent
        self._size = size

    def stat(self) -> _MockStat:
        return _MockStat(self._size)


def run_debug_demo(root_dir: Path):
    """Simulacion visual completa (instalacion, API key y todas las fases) sin gastar API ni escribir archivos."""
    setup_ui = PipelineUI(console)
    setup_ui.add_row("dep_audio", "Componentes de audio")
    setup_ui.add_row("dep_py", "Python")
    setup_ui.add_row("dep_env", "Entorno")
    setup_ui.set_progress(description="Preparando primera ejecucion...", total=3, completed=0)

    with Live(setup_ui.render(), console=console, refresh_per_second=8) as live:
        setup_ui.attach_live(live)
        for key in ("dep_audio", "dep_py", "dep_env"):
            setup_ui.update_row(key, "[accent]Instalando...[/]", "")
            time.sleep(1.3)
            setup_ui.update_row(key, "[ok]✓ Listo[/]", "")
            setup_ui.set_progress(advance=1)

    console.print(
        Panel(
            "Introduce tu clave de API de Google. Se guardara cifrada en este equipo.",
            title="[accent]Clave de API[/]",
            box=box.ROUNDED,
            border_style="cyan",
        )
    )
    if sys.stdin.isatty():
        console.print("[accent]Clave:[/] ", end="")
        try:
            input()
        except EOFError:
            pass
    else:
        console.print("[accent]Clave:[/] [muted]********[/]")
        time.sleep(1.0)
    console.clear()

    mock_audios = [
        _MockFile("clase_parte1.m4a", root_dir, int(22.4 * 1024 * 1024)),
        _MockFile("clase_parte2.mp4", root_dir, int(27.8 * 1024 * 1024)),
    ]
    mock_docs = [_MockFile("tema_apuntes.pdf", root_dir, int(1.8 * 1024 * 1024))]
    render_intro(root_dir, mock_audios, mock_docs, 2)

    ui = PipelineUI(console)
    ui.add_row("vocab", "Apuntes")
    for idx, audio in enumerate(mock_audios, 1):
        ui.add_row(f"audio_{idx}", escape(audio.name))

    with Live(ui.render(), console=console, refresh_per_second=8) as live:
        ui.attach_live(live)

        ui.set_progress(description="Paso 1/4 · Leyendo apuntes...", total=1, completed=0)
        ui.update_row("vocab", "[accent]Leyendo...[/]", "")
        time.sleep(1.5)
        ui.update_row("vocab", "[ok]✓ Listo[/]", "64 terminos")
        ui.set_progress(advance=1)

        ui.set_progress(description="Paso 2/4 · Procesando 2 audios...", total=2, completed=0)
        ui.update_row("audio_1", "[accent]Procesando 35%[/]", "")
        ui.update_row("audio_2", "[accent]Procesando 28%[/]", "")
        time.sleep(0.8)
        ui.update_row("audio_1", "[accent]Procesando 78%[/]", "")
        ui.update_row("audio_2", "[accent]Procesando 64%[/]", "")
        time.sleep(0.8)
        ui.update_row("audio_1", "[ok]✓ Listo[/]", "42:15")
        ui.set_progress(advance=1)
        time.sleep(1.2)
        ui.update_row("audio_2", "[ok]✓ Listo[/]", "51:30 (2 partes)")
        ui.set_progress(advance=1)

        for i in range(1, 4):
            ui.add_row(f"tx_{i}", f"Transcripcion ({i}/3)")
        ui.set_progress(description="Paso 3/4 · Transcribiendo...", total=3, completed=0)

        ui.update_row("tx_1", "[accent]Transcribiendo...[/]", "")
        time.sleep(1.6)
        ui.update_row("tx_1", "[ok]✓ Listo[/]", "4,815 palabras")
        ui.set_progress(advance=1)

        ui.update_row("tx_2", "[accent]Transcribiendo...[/]", "")
        time.sleep(1.5)
        ui.update_row("tx_2", "[warn]Reintentando (1/4) en 5s...[/]", "")
        time.sleep(1.8)
        ui.update_row("tx_2", "[warn]Cambiando de modelo...[/]", "")
        time.sleep(1.6)
        ui.update_row("tx_2", "[ok]✓ Listo[/]", "3,920 palabras")
        ui.set_progress(advance=1)

        ui.update_row("tx_3", "[accent]Transcribiendo...[/]", "")
        time.sleep(1.5)
        ui.update_row("tx_3", "[ok]✓ Listo[/]", "2,140 palabras")
        ui.set_progress(advance=1)

        ui.add_row("review", "Revision")
        ui.add_row("pdf", "PDF")
        ui.set_progress(description="Paso 4/4 · Revisando...", total=2, completed=0)
        ui.update_row("review", "[accent]Revisando...[/]", "")
        time.sleep(2.0)
        ui.update_row("review", "[ok]✓ Listo[/]", "")

        ui.set_progress(description="Paso 4/4 · Creando PDF...", advance=1)
        ui.update_row("pdf", "[accent]Creando PDF...[/]", "")
        time.sleep(1.5)
        ui.update_row("pdf", "[ok]✓ Listo[/]", "248 KB")
        ui.set_progress(description="Terminado", advance=1)

    mock_pdf = _MockFile("APUNTES_Ejemplo_Clase.pdf", root_dir, 248 * 1024)
    render_summary(mock_pdf, mock_audios, [1, 2, 3], [], [], 84.0, total_audio_sec=5625.0)


def main():
    global console
    t0 = time.time()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    args = build_parser().parse_args()
    is_tty = sys.stdout.isatty() and os.environ.get("TERM", "").lower() != "dumb" and not os.environ.get("CI")
    no_color = bool(os.environ.get("NO_COLOR")) or (not is_tty and not args.json_out)
    console = Console(
        theme=THEME,
        stderr=args.json_out,
        quiet=args.quiet or args.json_out,
        no_color=no_color,
    )

    if is_tty and not args.no_clear and not args.quiet and not args.json_out:
        console.clear()

    root_dir = Path(args.carpeta).resolve() if args.carpeta else Path.cwd()
    if not root_dir.is_dir():
        emit_error(
            _CLIError(
                code="INVALID_FOLDER",
                message=f"La carpeta no existe: {root_dir}",
                fix="Pasa una ruta de carpeta valida.",
                exit_code=EXIT_NOT_FOUND,
            ),
            args.json_out,
            console,
        )
        sys.exit(EXIT_NOT_FOUND)

    if args.debug:
        run_debug_demo(root_dir)
        sys.exit(EXIT_OK)

    runtime_dir = get_runtime_dir()
    pipe_dir = root_dir / ".clase_pipeline"
    dpapi_file = runtime_dir / "api_key.dpapi"

    if args.reset_key:
        _clear_saved_api_keys(runtime_dir, pipe_dir)
        if not args.json_out:
            console.print("[ok]✓[/] Claves de API borradas.")

    if args.add_api:
        non_interactive = (not sys.stdin.isatty()) or bool(os.environ.get("CI"))
        try:
            res = add_api_key_flow(runtime_dir, non_interactive=non_interactive, json_mode=args.json_out)
        except _CLIError as err:
            emit_error(err, args.json_out, console)
            sys.exit(err.exit_code)
        if args.json_out:
            emit_json("ok", data=res, warnings=[])
        sys.exit(EXIT_OK)

    if args.apis:
        non_interactive = (not sys.stdin.isatty()) or bool(os.environ.get("CI"))
        if non_interactive or args.json_out:
            emit_error(
                _CLIError(
                    "NON_INTERACTIVE",
                    "El menu de claves requiere una terminal interactiva.",
                    "Ejecuta --apis desde una consola interactiva.",
                    EXIT_CONFIG,
                ),
                args.json_out,
                console,
            )
            sys.exit(EXIT_CONFIG)
        manage_api_keys_menu(runtime_dir, pipe_dir)
        sys.exit(EXIT_OK)

    audios = sorted(
        [p for p in root_dir.iterdir() if p.is_file() and not p.name.startswith(("~$", ".")) and p.suffix.lower() in AUDIO_EXTS],
        key=natural_key,
    )
    base_notes = sorted(
        [p for p in root_dir.iterdir() if not p.name.startswith(("~$", ".")) and is_base_notes_pdf(p)],
        key=natural_key,
    )
    docs = sorted(
        [
            p for p in root_dir.iterdir()
            if p.is_file() and not p.name.startswith(("~$", ".")) and p.suffix.lower() in DOC_EXTS and not is_base_notes_pdf(p)
        ],
        key=natural_key,
    )
    context_docs = docs + base_notes

    if args.reset_key and not audios:
        if args.json_out:
            emit_json("ok", data={"key_reset": True}, warnings=[])
        sys.exit(EXIT_OK)

    if not audios:
        emit_error(
            _CLIError(
                code="NO_AUDIOS",
                message="No hay archivos de audio en esta carpeta.",
                fix="Copia al menos un audio (.mp3, .mp4, .m4a, .wav...) en la carpeta y vuelve a ejecutar.",
                exit_code=EXIT_NOT_FOUND,
            ),
            args.json_out,
            console,
        )
        sys.exit(EXIT_NOT_FOUND)

    max_workers = max(1, min(len(audios), max(1, (os.cpu_count() or 2) // 2), 4))

    if args.dry_run:
        if args.json_out:
            emit_json(
                "ok",
                data={
                    "dry_run": True,
                    "folder": str(root_dir),
                    "audios": [{"name": a.name, "size_bytes": a.stat().st_size} for a in audios],
                    "base_notes": [{"name": b.name, "size_bytes": b.stat().st_size} for b in base_notes],
                    "docs": [{"name": d.name, "size_bytes": d.stat().st_size} for d in docs],
                },
                warnings=[],
            )
        else:
            render_intro(root_dir, audios, docs, max_workers, target_console=console, base_notes=base_notes)
            console.print(
                Panel(
                    "Modo simulacion (--dry-run): no se proceso ningun archivo ni se llamo a la API.",
                    title="[accent]Simulacion[/]",
                    box=box.ROUNDED,
                    border_style="cyan",
                )
            )
        sys.exit(EXIT_OK)

    render_intro(root_dir, audios, docs, max_workers, target_console=console, base_notes=base_notes)

    non_interactive = (not sys.stdin.isatty()) or bool(os.environ.get("CI"))
    models_used: list[str] = []
    api_keys: list[tuple[int, str]] = []
    try:
        output_mode = resolve_output_mode(
            args.mode,
            non_interactive=non_interactive,
            json_mode=args.json_out,
            runtime_dir=runtime_dir,
            legacy_pipe_dir=pipe_dir,
        )
        api_keys = get_or_prompt_api_keys(runtime_dir, non_interactive=non_interactive, legacy_pipe_dir=pipe_dir)
        pipe_dir.mkdir(exist_ok=True)
        _hide_dir_windows(pipe_dir)

        pool = ClientPool(api_keys)

        ui = PipelineUI(console)
        if context_docs:
            ui.add_row("vocab", "Apuntes")
        for idx, audio in enumerate(audios, 1):
            ui.add_row(f"audio_{idx}", escape(audio.name))

        total_steps = 4 if context_docs else 3
        step_off = 1 if context_docs else 0
        live_ctx = (
            nullcontext(None)
            if (args.quiet or args.json_out)
            else Live(ui.render(), console=console, refresh_per_second=8)
        )
        with live_ctx as live:
            if live is not None:
                ui.attach_live(live)

            if context_docs:
                ui.set_progress(description=f"Paso 1/{total_steps} · Leyendo apuntes...", total=1, completed=0)
            vocab, vocab_model = extract_vocabulary(pool, context_docs, pipe_dir, ui)
            if vocab_model and vocab_model not in models_used:
                models_used.append(vocab_model)

            chunks, total_audio_sec = clean_and_split_audios(
                audios, pipe_dir, max_workers, ui, step_label=f"Paso {1 + step_off}/{total_steps}"
            )
            transcripts, tx_models = transcribe_chunks(
                pool, chunks, vocab, pipe_dir, ui, step_label=f"Paso {2 + step_off}/{total_steps}"
            )
            for m in tx_models:
                if m not in models_used:
                    models_used.append(m)

            total_tx_words = sum(len(t.split()) for t in transcripts)
            need_tx_topic_split = total_tx_words > LARGE_TRANSCRIPT_WORDS

            single_large_guide = output_mode == "apuntes" and len(docs) == 1 and is_large_pdf(docs[0])
            single_large_base = len(base_notes) == 1 and is_large_pdf(base_notes[0]) and not single_large_guide

            need_base_triage = not single_large_base and (len(base_notes) >= 2 or any(is_large_pdf(p) for p in base_notes))
            need_guide_triage = not single_large_guide and output_mode == "apuntes" and (
                len(docs) >= 2 or any(is_large_pdf(p) for p in docs)
            )
            has_triage = single_large_base or single_large_guide or need_base_triage or need_guide_triage or need_tx_topic_split

            selected_base = list(base_notes)
            selected_guides = list(docs)
            topic_jobs: list[dict] = []

            if has_triage:
                triage_label = (
                    "Dividiendo por temas"
                    if (single_large_base or single_large_guide or need_tx_topic_split)
                    else "Filtrando apuntes"
                )
                ui.add_row("triage", triage_label)
            ui.add_row("review", "Revision")
            ui.add_row("pdf", "PDF")

            final_tasks = 2 + (1 if has_triage else 0)
            ui.set_progress(
                description=f"Paso {3 + step_off}/{total_steps} · {'Dividiendo por temas...' if has_triage else 'Revisando...'}",
                total=final_tasks,
                completed=0,
            )

            if has_triage:
                triage_infos: list[str] = []
                if single_large_base:
                    jobs, b_info, b_models = plan_large_pdf_topics(
                        pool, base_notes[0], transcripts, pipe_dir, ui, row_key="triage"
                    )
                    for j in jobs:
                        topic_jobs.append({"base": [j["path"]], "guides": selected_guides, "style_only": j["style_only"], "title": j["title"]})
                    if b_info:
                        triage_infos.append(b_info)
                    for m in b_models:
                        if m not in models_used:
                            models_used.append(m)
                elif single_large_guide:
                    jobs, g_info, g_models = plan_large_pdf_topics(
                        pool, docs[0], transcripts, pipe_dir, ui, row_key="triage"
                    )
                    for j in jobs:
                        topic_jobs.append({"base": selected_base, "guides": [j["path"]], "style_only": j["style_only"], "title": j["title"]})
                    if g_info:
                        triage_infos.append(g_info)
                    for m in g_models:
                        if m not in models_used:
                            models_used.append(m)
                else:
                    if need_base_triage:
                        selected_base, b_info, b_models = select_and_slice_docs(
                            pool, base_notes, transcripts, pipe_dir, ui, row_key="triage"
                        )
                        if b_info:
                            triage_infos.append(b_info)
                        for m in b_models:
                            if m not in models_used:
                                models_used.append(m)
                    if need_guide_triage:
                        selected_guides, g_info, g_models = select_and_slice_docs(
                            pool, docs, transcripts, pipe_dir, ui, row_key="triage"
                        )
                        if g_info:
                            triage_infos.append(g_info)
                        for m in g_models:
                            if m not in models_used:
                                models_used.append(m)
                if not topic_jobs and need_tx_topic_split:
                    tx_jobs, tx_info, tx_m = plan_transcript_topics(
                        pool,
                        transcripts,
                        ui,
                        row_key="triage",
                        fallback_title=root_dir.name if root_dir.name else "clase",
                    )
                    for j in tx_jobs:
                        topic_jobs.append(
                            {
                                "base": selected_base,
                                "guides": selected_guides,
                                "style_only": False,
                                "title": j["title"],
                                "transcripts": j["transcripts"],
                            }
                        )
                    if tx_info:
                        triage_infos.append(tx_info)
                    for m in tx_m:
                        if m not in models_used:
                            models_used.append(m)
                ui.update_row("triage", "[ok]✓ Listo[/]", "; ".join(triage_infos))
                ui.set_progress(description=f"Paso {3 + step_off}/{total_steps} · Revisando...", advance=1)

            if not topic_jobs:
                topic_jobs = [{"base": selected_base, "guides": selected_guides, "style_only": False, "title": None}]

            fallback_name = root_dir.name if root_dir.name else "clase"
            topic_jobs = _enforce_job_transcript_budget(topic_jobs, transcripts, fallback_name)
            out_prefix = TRANSCRIPT_PREFIX if output_mode == "transcripcion" else OUTPUT_PREFIX
            topics_cache_dir = pipe_dir / "topics"
            topics_cache_dir.mkdir(exist_ok=True)
            synthesized: list[tuple[dict, str]] = []

            for j_idx, job in enumerate(topic_jobs, 1):
                if len(topic_jobs) > 1:
                    ui.update_row("review", f"[accent]Tema {j_idx}/{len(topic_jobs)}...[/]", "")
                job_txs = job.get("transcripts") or transcripts
                sig_payload = json.dumps(
                    {
                        "mode": output_mode,
                        "title": job.get("title"),
                        "style_only": job["style_only"],
                        "tx": job_txs,
                        "files": [(p.name, p.stat().st_mtime_ns) for p in (job["base"] + job["guides"]) if p.exists()],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                job_sig = hashlib.sha256(sig_payload.encode("utf-8")).hexdigest()[:12]
                topic_cache = topics_cache_dir / f"topic_{j_idx:02d}_{job_sig}.md"
                cached_md = ""
                if topic_cache.exists():
                    try:
                        cached_md = topic_cache.read_text(encoding="utf-8")
                    except OSError:
                        cached_md = ""
                if cached_md.strip():
                    md_text = cached_md
                else:
                    md_text, rev_model = synthesize_notes(
                        pool,
                        job_txs,
                        vocab,
                        job["title"] or fallback_name,
                        ui,
                        base_notes=job["base"],
                        mode=output_mode,
                        guide_docs=job["guides"],
                        style_only=job["style_only"],
                        topic_focus=job["title"],
                    )
                    if rev_model not in models_used:
                        models_used.append(rev_model)
                    t_tmp = topic_cache.with_suffix(".tmp.md")
                    t_tmp.write_text(md_text, encoding="utf-8")
                    os.replace(t_tmp, topic_cache)
                synthesized.append((job, md_text))

            ui.set_progress(description=f"Paso {3 + step_off}/{total_steps} · Creando PDF...", advance=1)
            generated_pdfs: list[Path] = []
            for j_idx, (job, md_text) in enumerate(synthesized, 1):
                if len(synthesized) > 1 and job["title"]:
                    base_name = derive_subject_slug(f"# {job['title']}", fallback_name)
                else:
                    base_name = derive_subject_slug(md_text, job["title"] or fallback_name)
                pdf_out = unique_output_path(root_dir / f"{out_prefix}{base_name}.pdf")
                ui.update_row("pdf", "[accent]Creando PDF...[/]", f"{j_idx}/{len(synthesized)}" if len(synthesized) > 1 else "")
                export_pdf(md_text, pipe_dir, pdf_out)
                if args.keep_md:
                    pdf_out.with_suffix(".md").write_text(md_text, encoding="utf-8")
                generated_pdfs.append(pdf_out)

            total_kb = sum(p.stat().st_size for p in generated_pdfs) / 1024
            pdf_info = f"{len(generated_pdfs)} PDFs ({total_kb:.0f} KB)" if len(generated_pdfs) > 1 else f"{total_kb:.0f} KB"
            ui.update_row("pdf", "[ok]✓ Listo[/]", pdf_info)
            ui.set_progress(description="Terminado", advance=1)

        cleanup_intermediate(pipe_dir)
        elapsed = time.time() - t0
        if args.json_out:
            emit_json(
                "ok",
                data={
                    "pdf": str(generated_pdfs[0]),
                    "pdfs": [str(p) for p in generated_pdfs],
                    "mode": output_mode,
                    "size_bytes": sum(p.stat().st_size for p in generated_pdfs),
                    "audios": len(audios),
                    "audio_duration_sec": round(total_audio_sec, 1),
                    "chunks": len(chunks),
                    "elapsed_sec": round(elapsed, 2),
                },
                warnings=[],
            )
        else:
            render_summary(
                generated_pdfs if len(generated_pdfs) > 1 else generated_pdfs[0],
                audios, chunks, vocab, models_used, elapsed,
                target_console=console, total_audio_sec=total_audio_sec, mode=output_mode,
            )
        sys.exit(EXIT_OK)
    except KeyboardInterrupt:
        emit_error(
            _CLIError(
                code="CANCELLED",
                message="Cancelado. El progreso guardado se conserva.",
                fix="Vuelve a ejecutar para continuar.",
                exit_code=EXIT_CANCELLED,
            ),
            args.json_out,
            console,
        )
        sys.exit(EXIT_CANCELLED)
    except _CLIError as err:
        emit_error(err, args.json_out, console)
        sys.exit(err.exit_code)
    except Exception as exc:
        msg = str(exc).lower()
        if (
            any(
                tok in msg
                for tok in (
                    "api_key_invalid",
                    "invalid api key",
                    "api key not valid",
                    "illegal header",
                    "invalid header",
                    "401",
                )
            )
            and not os.environ.get("GEMINI_API_KEY")
        ):
            if len(api_keys) <= 1 and dpapi_file.exists():
                dpapi_file.unlink(missing_ok=True)
                err_msg = "Clave de API no valida. Se ha borrado la clave guardada."
                err_fix = "Vuelve a ejecutar para introducir una clave valida."
            else:
                err_msg = "Ninguna clave de API guardada paso la autenticacion."
                err_fix = "Usa --reset-key para limpiarlas o --add-api para añadir una nueva."
            emit_error(
                _CLIError(
                    code="AUTH_FAILED",
                    message=err_msg,
                    fix=err_fix,
                    exit_code=EXIT_AUTH,
                ),
                args.json_out,
                console,
            )
            sys.exit(EXIT_AUTH)
        if _classify_error(exc) in ("transient", "server_busy", "quota_daily", "rate_limit"):
            reason = _transient_reason(exc)
            if reason.startswith("429 limite de tokens"):
                fix_msg = "La peticion supero el limite de tokens/minuto (TPM). Espera 1 minuto o divide los audios (el progreso guardado se reutiliza)."
            elif reason.startswith("429"):
                fix_msg = "Añade otra clave con --add-api o espera al reinicio de cuota (el progreso guardado se reutiliza)."
            else:
                fix_msg = "Espera unos minutos y vuelve a ejecutar (el progreso guardado se reutiliza)."
            emit_error(
                _CLIError(
                    code="API_UNAVAILABLE",
                    message=f"No se pudo completar la peticion ({reason}).",
                    fix=fix_msg,
                    exit_code=EXIT_TRANSIENT,
                    transient=True,
                ),
                args.json_out,
                console,
            )
            sys.exit(EXIT_TRANSIENT)
        if isinstance(exc, FileNotFoundError) and "edge" in msg:
            emit_error(
                _CLIError(
                    code="BROWSER_NOT_FOUND",
                    message=str(exc),
                    fix="Instala Microsoft Edge o Google Chrome.",
                    exit_code=EXIT_CONFIG,
                ),
                args.json_out,
                console,
            )
            sys.exit(EXIT_CONFIG)
        log_path = _write_error_log(exc)
        short_msg = f"{exc.__class__.__name__}: {(str(exc).splitlines() or ['Error desconocido'])[0][:200]}"
        emit_error(
            _CLIError(
                code="UNEXPECTED_ERROR",
                message=short_msg,
                fix=f"Ejecuta de nuevo o comparte este codigo. Detalle en {log_path.name}" if log_path else "Ejecuta de nuevo o revisa los archivos de entrada.",
                exit_code=EXIT_ERROR,
            ),
            args.json_out,
            console,
        )
        sys.exit(EXIT_ERROR)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        emit_error(
            _CLIError("CANCELLED", "Cancelado por el usuario.", "Vuelve a ejecutar para continuar.", EXIT_CANCELLED),
            "--json" in sys.argv,
            console,
        )
        sys.exit(EXIT_CANCELLED)
    except Exception as exc:
        log_path = _write_error_log(exc)
        short_msg = f"{exc.__class__.__name__}: {(str(exc).splitlines() or ['Error desconocido'])[0][:200]}"
        emit_error(
            _CLIError(
                "FATAL_ERROR",
                short_msg,
                f"Comparte este codigo para revisarlo ({log_path.name})." if log_path else "Vuelve a intentarlo.",
                EXIT_ERROR,
            ),
            "--json" in sys.argv,
            console,
        )
        sys.exit(EXIT_ERROR)

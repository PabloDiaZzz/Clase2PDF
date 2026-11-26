import json
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BAT = str(REPO_ROOT / "procesar_clase.bat")
ENV = {**os.environ, "CLASE2PDF_NO_UPDATE": "1"}

# 1. --version
r_ver = subprocess.run(["cmd", "/c", BAT, "--version"], capture_output=True, text=True, encoding="utf-8", env=ENV)
assert r_ver.returncode == 0, f"--version failed: {r_ver.returncode} {r_ver.stderr}"
assert "Clase2PDF 1.9.2" in r_ver.stdout, r_ver.stdout

# 2. --help
r_help = subprocess.run(["cmd", "/c", BAT, "--help"], capture_output=True, text=True, encoding="utf-8", env=ENV)
assert r_help.returncode == 0, f"--help failed: {r_help.returncode}"
for flag in ("EJEMPLOS:", "--dry-run", "--json", "--add-api", "--apis", "--keep-md", "--mode"):
    assert flag in r_help.stdout, f"Missing {flag} in --help"

# 3. --dry-run --json (on a temp dir with an audio)
dry_dir = REPO_ROOT / "tests" / "_dry_test"
dry_dir.mkdir(exist_ok=True)
try:
    (dry_dir / "clase1.mp3").write_bytes(b"fake")
    r_dry = subprocess.run(["cmd", "/c", BAT, str(dry_dir), "--dry-run", "--json"], capture_output=True, text=True, encoding="utf-8", env=ENV)
    assert r_dry.returncode == 0, f"--dry-run --json failed: {r_dry.returncode} {r_dry.stdout} {r_dry.stderr}"
    assert "\x1b" not in r_dry.stdout, "ANSI escape code leaked into --json stdout!"
    data_dry = json.loads(r_dry.stdout)
    assert data_dry["status"] == "ok" and data_dry["data"]["dry_run"] is True and len(data_dry["data"]["audios"]) == 1
    assert not (dry_dir / ".clase_pipeline").exists()
finally:
    shutil.rmtree(dry_dir, ignore_errors=True)

# 4. Empty/no-audio folder with --json -> exit 3 (EXIT_NOT_FOUND)
empty_dir = REPO_ROOT / "tests" / "_empty_test"
empty_dir.mkdir(exist_ok=True)
try:
    r_err = subprocess.run(["cmd", "/c", BAT, str(empty_dir), "--json"], capture_output=True, text=True, encoding="utf-8", env=ENV)
    assert r_err.returncode == 3, f"Expected exit 3, got {r_err.returncode}: {r_err.stdout} {r_err.stderr}"
    assert "\x1b" not in r_err.stdout
    data_err = json.loads(r_err.stdout)
    assert data_err["status"] == "error" and data_err["error"]["code"] == "NO_AUDIOS" and data_err["error"]["fix"]
    assert not (empty_dir / ".clase_pipeline").exists()
finally:
    shutil.rmtree(empty_dir, ignore_errors=True)

assert (Path(os.environ["LOCALAPPDATA"]) / "Clase2PDF" / "venv" / "Scripts" / "python.exe").exists()

# 5. Base notes detection (Apuntes_*.pdf case-insensitive) in --dry-run --json
base_dir = REPO_ROOT / "tests" / "_base_notes_test"
base_dir.mkdir(exist_ok=True)
try:
    (base_dir / "audio3.mp3").write_bytes(b"fake")
    (base_dir / "Apuntes_previos.pdf").write_bytes(b"%PDF-1.4")
    (base_dir / "tema_contexto.pdf").write_bytes(b"%PDF-1.4")
    r_base = subprocess.run(["cmd", "/c", BAT, str(base_dir), "--dry-run", "--json"], capture_output=True, text=True, encoding="utf-8", env=ENV)
    assert r_base.returncode == 0, f"base_notes dry-run failed: {r_base.returncode} {r_base.stdout} {r_base.stderr}"
    d_base = json.loads(r_base.stdout)["data"]
    assert [x["name"] for x in d_base["base_notes"]] == ["Apuntes_previos.pdf"], d_base
    assert [x["name"] for x in d_base["docs"]] == ["tema_contexto.pdf"], d_base
finally:
    shutil.rmtree(base_dir, ignore_errors=True)

print("CLI_CONTRACT_OK")

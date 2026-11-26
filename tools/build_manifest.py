import hashlib
import json
import os
import re
import shutil
from pathlib import Path

REQUIREMENTS = ["google-genai", "rich", "questionary", "pypdf", "fpdf2"]


def _sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().lower()


def _extract_version(pipeline_path: Path) -> str:
    txt = pipeline_path.read_text(encoding="utf-8")
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', txt, re.MULTILINE)
    return m.group(1) if m else "1.0.0"


def main():
    repo_root = Path(__file__).resolve().parent.parent
    pipeline_path = repo_root / "pipeline.py"
    bat_path = repo_root / "procesar_clase.bat"
    ins_path = repo_root / "INSTRUCCIONES.md"

    # Normalizar finales de linea para coincidir 100% con .gitattributes
    pipeline_bytes = pipeline_path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
    pipeline_path.write_bytes(pipeline_bytes)

    bat_text = bat_path.read_text(encoding="ascii").replace("\r\n", "\n").replace("\n", "\r\n")
    bat_path.write_bytes(bat_text.encode("ascii"))

    ins_sha = ""
    if ins_path.exists():
        ins_bytes = ins_path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
        ins_path.write_bytes(ins_bytes)
        ins_sha = _sha256_hex(ins_path)

    pipe_sha = _sha256_hex(pipeline_path)
    bat_sha = _sha256_hex(bat_path)
    req_hash = hashlib.sha256(" ".join(REQUIREMENTS).encode("utf-8")).hexdigest()[:16]

    manifest = {
        "version": _extract_version(pipeline_path),
        "pipeline_sha256": pipe_sha,
        "bat_sha256": bat_sha,
        "req_hash": req_hash,
        "requirements": REQUIREMENTS,
    }
    if ins_sha:
        manifest["instructions_sha256"] = ins_sha
    manifest_path = repo_root / "manifest.json"
    manifest_path.write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))

    # Sincronizar copia local en OneDrive y %LOCALAPPDATA%\Clase2PDF
    onedrive_bat = Path(r"C:\Users\pablo\OneDrive\Desktop\Clase2PDF\procesar_clase.bat")
    if onedrive_bat.parent.exists():
        shutil.copyfile(bat_path, onedrive_bat)
        if ins_path.exists():
            shutil.copyfile(ins_path, onedrive_bat.parent / "INSTRUCCIONES.md")

    local_rt = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "Clase2PDF"
    local_rt.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pipeline_path, local_rt / "pipeline.py")
    (local_rt / ".pipe_hash").write_text(pipe_sha[:16] + "\n", encoding="ascii")
    (local_rt / ".req_pkgs").write_text(" ".join(REQUIREMENTS) + "\n", encoding="ascii")
    (local_rt / ".req_hash_want").write_text(req_hash + "\n", encoding="ascii")
    (local_rt / ".req_hash_done").write_text(req_hash + "\n", encoding="ascii")

    print(
        f"MANIFEST_OK v={manifest['version']} pipe={pipe_sha[:12]} bat={bat_sha[:12]} "
        f"bat_lines={len(bat_text.splitlines())}"
    )


if __name__ == "__main__":
    main()

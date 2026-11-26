import json
import os
import shutil
import threading
import time
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import pipeline


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def translate_path(self, path):
        clean = urllib.parse.urlsplit(path).path
        return super().translate_path(clean)


# 1. Bug #1: 503 server_busy skips rotating to API 2; 429 quota_daily rotates to API 2 first
calls = []
class _FakeModels:
    def __init__(self, owner, mode):
        self.owner = owner
        self.mode = mode

    def generate_content(self, model, contents, config=None):
        calls.append((self.owner, model))
        if self.mode == "503" and model == "gemini-4-hydrogen":
            raise RuntimeError("503 UNAVAILABLE high demand")
        if self.mode == "429" and self.owner == "API1" and model == "gemini-4-hydrogen":
            raise RuntimeError("429 RESOURCE_EXHAUSTED RequestsPerDay")
        return type("Resp", (), {"text": f"ok:{self.owner}:{model}"})()

class _FakeClient:
    def __init__(self, owner, mode):
        self.models = _FakeModels(owner, mode)

orig_defaults = pipeline.call_with_retry.__defaults__
pipeline.call_with_retry.__defaults__ = ((0,), None)
try:
    pool_503 = pipeline.ClientPool(
        [(1, "AIzaSyKeyOne000000001"), (2, "AIzaSyKeyTwo000000002")],
        client_factory=lambda k: _FakeClient("API1" if "One" in k else "API2", "503"),
    )
    _, mod_503 = pipeline.generate_with_fallback(pool_503, "p", None, "503Test", on_status=lambda m: None)
    assert mod_503 == "gemini-3.8-flash"
    assert calls == [("API1", "gemini-4-hydrogen"), ("API1", "gemini-4-hydrogen"), ("API1", "gemini-3.8-flash")], calls

    calls.clear()
    pool_429 = pipeline.ClientPool(
        [(1, "AIzaSyKeyOne000000001"), (2, "AIzaSyKeyTwo000000002")],
        client_factory=lambda k: _FakeClient("API1" if "One" in k else "API2", "429"),
    )
    _, mod_429 = pipeline.generate_with_fallback(pool_429, "p", None, "429Test", on_status=lambda m: None)
    assert mod_429 == "gemini-4-hydrogen"
    assert calls == [("API1", "gemini-4-hydrogen"), ("API2", "gemini-4-hydrogen")], calls
finally:
    pipeline.call_with_retry.__defaults__ = orig_defaults

# 2. Bug #3 & #7: fingerprint stability + atomic write cleans .tmp.mp3 on ffmpeg failure
tmp_root = REPO_ROOT / "tests" / "_tmp_v19"
shutil.rmtree(tmp_root, ignore_errors=True)
tmp_root.mkdir(parents=True, exist_ok=True)
try:
    fake_a = tmp_root / "clase.mp3"
    fake_a.write_bytes(b"not_an_audio")
    fp1 = pipeline._file_fingerprint(fake_a)
    fp2 = pipeline._file_fingerprint(fake_a)
    assert len(fp1) == 10 and fp1 == fp2

    from rich.console import Console
    ui = pipeline.PipelineUI(Console(quiet=True))
    ui.add_row("audio_1", "clase.mp3")
    failed = False
    try:
        pipeline._process_one_audio(1, fake_a, tmp_root / "clean", tmp_root / "chunks", ui)
    except Exception:
        failed = True
    assert failed
    assert not list(tmp_root.glob("clean/*.mp3")), "Partial or tmp mp3 left behind after ffmpeg failure"

    # 3. Bug #4: malformed JSON in extract_vocabulary does not crash
    orig_gwf = pipeline.generate_with_fallback
    try:
        pipeline.generate_with_fallback = lambda *a, **kw: (type("R", (), {"text": "[malformed json,"})(), "gemini-3.8-flash")
        (tmp_root / "g.docx").write_bytes(b"PK")
        import zipfile
        with zipfile.ZipFile(tmp_root / "g.docx", "w") as zf:
            zf.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Hola</w:t></w:r></w:p></w:body></w:document>')
        terms, _ = pipeline.extract_vocabulary(pool_429, [tmp_root / "g.docx"], tmp_root, ui)
        assert terms == []
    finally:
        pipeline.generate_with_fallback = orig_gwf

    # 4. Bug #6: _PDF_PAGES_CACHE caches parsed PDFs
    import pypdf
    pdf_p = tmp_root / "cache_test.pdf"
    w = pypdf.PdfWriter()
    w.add_blank_page(width=200, height=200)
    with pdf_p.open("wb") as fh:
        w.write(fh)
    p1 = pipeline.inspect_pdf_pages(pdf_p)
    p2 = pipeline.inspect_pdf_pages(pdf_p)
    assert p1 is p2

    # 5. End-to-end auto-updater test via local http.server (download + SHA-256 + offline fallback)
    srv_dir = tmp_root / "srv"
    srv_dir.mkdir()
    for fn in ("pipeline.py", "procesar_clase.bat", "manifest.json", "INSTRUCCIONES.md"):
        shutil.copyfile(REPO_ROOT / fn, srv_dir / fn)

    handler = partial(_QuietHandler, directory=str(srv_dir))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    rt_parent = tmp_root / "localappdata"
    rt_dir = rt_parent / "Clase2PDF"
    rt_dir.mkdir(parents=True)
    # Symlink/junction existing venv so the test doesn't wait 3m reinstalling packages
    real_venv = Path(os.environ["LOCALAPPDATA"]) / "Clase2PDF" / "venv"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(rt_dir / "venv"), str(real_venv)], check=True, capture_output=True)

    bat_copy = tmp_root / "procesar_clase.bat"
    shutil.copyfile(REPO_ROOT / "procesar_clase.bat", bat_copy)

    test_env = {
        **os.environ,
        "LOCALAPPDATA": str(rt_parent),
        "CLASE2PDF_REPO_URL": f"http://127.0.0.1:{port}",
    }
    test_env.pop("CLASE2PDF_NO_UPDATE", None)

    try:
        r_up = subprocess.run(["cmd", "/c", str(bat_copy), "--version"], capture_output=True, text=True, encoding="utf-8", env=test_env)
        assert r_up.returncode == 0, f"Auto-update failed: {r_up.returncode} {r_up.stdout} {r_up.stderr}"
        assert "Clase2PDF 1.9.0" in r_up.stdout
        assert (rt_dir / "pipeline.py").exists() and (rt_dir / ".pipe_hash").exists()
        assert (tmp_root / "INSTRUCCIONES.md").exists() and not (rt_dir / "INSTRUCCIONES.md").exists()
    finally:
        httpd.shutdown()
        httpd.server_close()

    # Offline fallback after server stopped (pipeline.py already cached)
    r_off = subprocess.run(["cmd", "/c", str(bat_copy), "--version"], capture_output=True, text=True, encoding="utf-8", env=test_env)
    assert r_off.returncode == 0 and "Clase2PDF 1.9.0" in r_off.stdout, f"Offline fallback failed: {r_off.returncode} {r_off.stderr}"

    # Remove junction safely before rmtree
    subprocess.run(["cmd", "/c", "rmdir", str(rt_dir / "venv")], check=True, capture_output=True)
finally:
    shutil.rmtree(tmp_root, ignore_errors=True)

print("V19_VERIFY_OK")

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

    # 4b. fpdf2 fallback when Edge/Chrome is unavailable
    orig_fb = pipeline.find_browser
    try:
        def _no_browser():
            raise FileNotFoundError("no browser")
        pipeline.find_browser = _no_browser
        fb_pdf = tmp_root / "fallback_fpdf2.pdf"
        pipeline.export_pdf("# Tema Prueba\n\nTexto con acentos: camión, pingüino.\n\n- Punto uno\n", tmp_root, fb_pdf)
        assert fb_pdf.exists() and fb_pdf.stat().st_size > 500
        rd_fb = pypdf.PdfReader(str(fb_pdf))
        assert len(rd_fb.pages) >= 1
    finally:
        pipeline.find_browser = orig_fb

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
        assert "Clase2PDF 1.9.2" in r_up.stdout
        assert (rt_dir / "pipeline.py").exists() and (rt_dir / ".pipe_hash").exists()
        assert (tmp_root / "INSTRUCCIONES.md").exists() and not (rt_dir / "INSTRUCCIONES.md").exists()
    finally:
        httpd.shutdown()
        httpd.server_close()

    # Offline fallback after server stopped (pipeline.py already cached)
    r_off = subprocess.run(["cmd", "/c", str(bat_copy), "--version"], capture_output=True, text=True, encoding="utf-8", env=test_env)
    assert r_off.returncode == 0 and "Clase2PDF 1.9.2" in r_off.stdout, f"Offline fallback failed: {r_off.returncode} {r_off.stderr}"

    # 6. v1.9.1: repetition-loop cleaner
    w_loop = "Inicio de clase. " + ("silencio " * 12) + "Fin."
    cw, dw = pipeline._clean_repetition_loops(w_loop)
    assert dw == 11 and cw == "Inicio de clase. silencio Fin.", (cw, dw)

    p_loop = "Tema uno. " + ("es decir esto " * 5) + "Continuamos con la explicacion."
    cp, dp = pipeline._clean_repetition_loops(p_loop)
    assert dp == 12 and "Continuamos con la explicacion." in cp, (cp, dp)

    l_loop = "\n".join(["Linea normal uno.", "Linea repetida."] + ["Linea repetida."] * 4 + ["Linea normal dos."])
    cl, dl = pipeline._clean_repetition_loops(l_loop)
    assert dl == 8 and cl.splitlines() == ["Linea normal uno.", "Linea repetida.", "Linea normal dos."], (cl, dl)

    normal = "La célula eucariota posee núcleo definido. Además, las mitocondrias producen ATP."
    cn, dn = pipeline._clean_repetition_loops(normal)
    assert dn == 0 and cn == normal

    # 7. v1.9.1: cached looped transcript triggers retry with temperature=0.2
    tx_test_dir = tmp_root / "tx_retry"
    (tx_test_dir / "transcripts").mkdir(parents=True, exist_ok=True)
    fake_chunk = tx_test_dir / "b1.mp3"
    fake_chunk.write_bytes(b"fake")
    (tx_test_dir / "transcripts" / "b1.txt").write_text("hola " * 20000, encoding="utf-8")

    orig_up = pipeline._upload_local_file
    orig_wait = pipeline.wait_file_active
    orig_gwf2 = pipeline.generate_with_fallback
    retry_configs = []
    try:
        pipeline._upload_local_file = lambda c, p, on_status=None: type("F", (), {"name": "files/fake"})()
        pipeline.wait_file_active = lambda c, f, on_status=None: f
        def _fake_gwf_retry(pool_arg, contents, config, label, **kw):
            retry_configs.append(config)
            return type("R", (), {"text": "Transcripcion limpia y completa de la clase."})(), "gemini-4-hydrogen"
        pipeline.generate_with_fallback = _fake_gwf_retry

        txs_out, _ = pipeline.transcribe_chunks(pool_429, [fake_chunk], [], tx_test_dir, ui)
        assert len(retry_configs) == 1
        assert getattr(retry_configs[0], "temperature", None) == 0.2
        assert txs_out == ["Transcripcion limpia y completa de la clase."]
        assert (tx_test_dir / "transcripts" / "b1.txt").read_text(encoding="utf-8") == "Transcripcion limpia y completa de la clase."
    finally:
        pipeline._upload_local_file = orig_up
        pipeline.wait_file_active = orig_wait
        pipeline.generate_with_fallback = orig_gwf2

    # 8. v1.9.1: plan_transcript_topics zero-loss coverage & _enforce_job_transcript_budget
    orig_gwf3 = pipeline.generate_with_fallback
    try:
        # Mock LLM intentionally omits block 2 -> zero-loss guard must still include all 3 blocks
        pipeline.generate_with_fallback = lambda *a, **kw: (
            type("R", (), {"text": '{"topics": [{"title": "Tema A", "blocks": [1]}, {"title": "Tema B", "blocks": [3]}]}'})(),
            "gemini-3.6-flash",
        )
        blocks_in = ["uno " * 100, "dos " * 100, "tres " * 100]
        jobs_tx, _, _ = pipeline.plan_transcript_topics(pool_429, blocks_in, ui, max_words_per_topic=500)
        assert len(jobs_tx) >= 2
        recovered_words = sum(len(t.split()) for j in jobs_tx for t in j["transcripts"])
        assert recovered_words == 300, recovered_words

        big_job = [{"base": [], "guides": [], "style_only": False, "title": "Bloque Gigante", "transcripts": ["palabra " * 250]}]
        enforced = pipeline._enforce_job_transcript_budget(big_job, [], "clase", max_words=100)
        assert len(enforced) == 3
        assert sum(len(t.split()) for j in enforced for t in j["transcripts"]) == 250
        assert enforced[0]["title"].endswith("(Parte 1)")
    finally:
        pipeline.generate_with_fallback = orig_gwf3

    # 9. v1.9.1: TPM 429 diagnostic message
    assert pipeline._transient_reason(RuntimeError("429 RESOURCE_EXHAUSTED input_token_count per minute")) == "429 limite de tokens por minuto (TPM)"

    # 10. v1.9.2: fast 429 RPM rotation without retrying API 1 when API 2 exists
    rpm_calls = []
    class _RpmModels:
        def __init__(self, owner):
            self.owner = owner
        def generate_content(self, model, contents, config=None):
            rpm_calls.append((self.owner, model))
            if self.owner == "API1":
                raise RuntimeError("429 RESOURCE_EXHAUSTED rate limit")
            return type("Resp", (), {"text": "ok"})()
    pool_rpm = pipeline.ClientPool(
        [(1, "AIzaSyKeyOne000000001"), (2, "AIzaSyKeyTwo000000002")],
        client_factory=lambda k: type("C", (), {"models": _RpmModels("API1" if "One" in k else "API2")})(),
    )
    _, rpm_mod = pipeline.generate_with_fallback(pool_rpm, "p", None, "RpmTest", on_status=lambda m: None)
    assert rpm_mod == "gemini-4-hydrogen"
    assert rpm_calls == [("API1", "gemini-4-hydrogen"), ("API2", "gemini-4-hydrogen")], rpm_calls

    # 11. v1.9.2: corrupt/lock docx returns "" cleanly instead of crashing
    bad_docx = tmp_root / "~$corrupt.docx"
    bad_docx.write_bytes(b"not a zip")
    assert pipeline.read_docx_text(bad_docx) == ""

    # 12. v1.9.2: md_to_html renders fenced code blocks as <pre><code>
    code_html = pipeline.md_to_html("```python\nx = 1 < 2\n```")
    assert "<pre><code>x = 1 &lt; 2</code></pre>" in code_html, code_html

    # 13. v1.9.2: derive_subject_slug sanitizes illegal Windows chars in fallback
    assert pipeline.derive_subject_slug("Sin titulo H1", 'Tema 1: A/B*?"<>|') == "Tema_1_AB"

    # 14. v1.9.2: export_pdf falls back to fpdf2 when browser times out
    orig_run = pipeline.subprocess.run
    orig_fb2 = pipeline.find_browser
    try:
        pipeline.find_browser = lambda: "msedge.exe"
        def _timeout_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="msedge", timeout=120)
        pipeline.subprocess.run = _timeout_run
        to_pdf = tmp_root / "timeout_fallback.pdf"
        pipeline.export_pdf("# Prueba Timeout\n\nContenido.", tmp_root, to_pdf)
        assert to_pdf.exists() and to_pdf.stat().st_size > 500
    finally:
        pipeline.subprocess.run = orig_run
        pipeline.find_browser = orig_fb2

    # Remove junction safely before rmtree
    subprocess.run(["cmd", "/c", "rmdir", str(rt_dir / "venv")], check=True, capture_output=True)
finally:
    shutil.rmtree(tmp_root, ignore_errors=True)

print("V19_VERIFY_OK")

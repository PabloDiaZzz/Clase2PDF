import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(os.environ["LOCALAPPDATA"]) / "Clase2PDF"
sys.path.insert(0, str(REPO_ROOT))
import pipeline

# 1. Native ctypes DPAPI round-trip
sample = b"test_secret_12345"
enc = pipeline._dpapi_protect(sample)
dec = pipeline._dpapi_unprotect(enc)
assert dec == sample, "DPAPI round-trip mismatch"

# 2. Existing api_key.dpapi compatibility (never print the key)
dpapi_path = RUNTIME_DIR / "api_key.dpapi"
if dpapi_path.exists():
    existing = pipeline._dpapi_unprotect(dpapi_path.read_bytes()).decode("utf-8").strip()
    assert isinstance(existing, str) and len(existing) > 10, "Existing DPAPI decrypt failed"

# 2b. Auto-migration from legacy_pipe_dir & cleanup_intermediate removes empty pipe_dir
mig_tmp = Path(r"C:\Users\pablo\.gemini\antigravity\brain\cac239b5-6525-4158-9902-758e46192b38\scratch\_mig_test")
mig_rt = mig_tmp / "rt"
mig_leg = mig_tmp / "leg"
mig_rt.mkdir(parents=True, exist_ok=True)
mig_leg.mkdir(parents=True, exist_ok=True)
try:
    (mig_leg / "api_key.dpapi").write_bytes(pipeline._dpapi_protect(b"migrated_key_abc"))
    orig_env = os.environ.pop("GEMINI_API_KEY", None)
    try:
        got = pipeline.get_or_prompt_api_key(mig_rt, non_interactive=True, legacy_pipe_dir=mig_leg)
    finally:
        if orig_env is not None:
            os.environ["GEMINI_API_KEY"] = orig_env
    assert got == "migrated_key_abc"
    assert (mig_rt / "api_key.dpapi").exists()
    assert not (mig_leg / "api_key.dpapi").exists()
    (mig_leg / "clean").mkdir()
    pipeline.cleanup_intermediate(mig_leg)
    assert not mig_leg.exists()

    # 2c. Sanitization, format validation, corrupt DPAPI auto-discard, console flush
    assert pipeline._sanitize_api_key('\x1b[200~"AIzaSyTest1234567890abcd"\x1b[201~\r\n') == "AIzaSyTest1234567890abcd"
    assert not pipeline._is_valid_api_key_format("\x16")
    assert pipeline._is_valid_api_key_format("AIzaSyTest1234567890abcd")
    pipeline._flush_console_input()

    corrupt_rt = mig_tmp / "corrupt_rt"
    corrupt_rt.mkdir(parents=True, exist_ok=True)
    (corrupt_rt / "api_key.dpapi").write_bytes(pipeline._dpapi_protect(b"\x16"))
    orig_env2 = os.environ.pop("GEMINI_API_KEY", None)
    try:
        raised_missing = False
        try:
            pipeline.get_or_prompt_api_key(corrupt_rt, non_interactive=True)
        except pipeline._CLIError as exc:
            raised_missing = exc.code == "MISSING_API_KEY"
        assert raised_missing, "Corrupt DPAPI should be discarded and raise MISSING_API_KEY"
        assert not (corrupt_rt / "api_key.dpapi").exists()
    finally:
        if orig_env2 is not None:
            os.environ["GEMINI_API_KEY"] = orig_env2
    # 2d. emit_error renders error code and exit code cleanly
    from rich.console import Console
    err_rec = Console(record=True, width=100, theme=pipeline.THEME)
    pipeline.emit_error(
        pipeline._CLIError("AUTH_FAILED", "Clave no valida", "Introduce otra", pipeline.EXIT_AUTH),
        json_mode=False,
        target_console=err_rec,
    )
    err_out = err_rec.export_text()
    assert "Error (AUTH_FAILED)" in err_out and "Codigo: AUTH_FAILED (exit 4)" in err_out

    # 2e. Live context instantiation smoke check (regression for Live.__init__ kwargs)
    from rich.live import Live
    ui_live = pipeline.PipelineUI(err_rec)
    with Live(ui_live.render(), console=err_rec, refresh_per_second=8) as live_obj:
        ui_live.attach_live(live_obj)

    # 2f. is_base_notes_pdf + render_intro base_notes branch
    bn_dir = mig_tmp / "bn"
    bn_dir.mkdir(parents=True, exist_ok=True)
    p_base = bn_dir / "Apuntes_clase1.pdf"
    p_doc = bn_dir / "tema.pdf"
    p_docx = bn_dir / "Apuntes_no.docx"
    p_base.write_bytes(b"%PDF-1.4")
    p_doc.write_bytes(b"%PDF-1.4")
    p_docx.write_bytes(b"PK")
    assert pipeline.is_base_notes_pdf(p_base) is True
    assert pipeline.is_base_notes_pdf(p_doc) is False
    assert pipeline.is_base_notes_pdf(p_docx) is False
    bn_rec = Console(record=True, width=100, theme=pipeline.THEME)
    pipeline.render_intro(bn_dir, [p_doc], [p_doc], 1, target_console=bn_rec, base_notes=[p_base])
    assert "Apuntes base (1)" in bn_rec.export_text()

    # 2g. Multi-API (--add-api) slot numbering, test gate, duplicate guard & backwards compatibility
    multi_rt = mig_tmp / "multi_rt"
    multi_rt.mkdir(parents=True, exist_ok=True)
    orig_prompt = pipeline._prompt_api_key_interactive
    orig_test = pipeline.test_api_key
    orig_env3 = os.environ.pop("GEMINI_API_KEY", None)
    try:
        seq = iter(["AIzaSyFirstKey00000001", "AIzaSySecondKey0000002", "AIzaSyFirstKey00000001", "AIzaSyBadKey0000000003"])
        pipeline._prompt_api_key_interactive = lambda **kw: next(seq)
        pipeline.test_api_key = lambda k: (False, "400 API_KEY_INVALID") if "BadKey" in k else (True, "")

        r1 = pipeline.add_api_key_flow(multi_rt, non_interactive=False, json_mode=True)
        assert r1 == {"slot": 1, "duplicate": False, "verified": True, "total_keys": 1}, r1
        assert (multi_rt / "api_key.dpapi").exists()

        r2 = pipeline.add_api_key_flow(multi_rt, non_interactive=False, json_mode=True)
        assert r2 == {"slot": 2, "duplicate": False, "verified": True, "total_keys": 2}, r2
        assert (multi_rt / "api_key_2.dpapi").exists()
        assert pipeline.get_or_prompt_api_key(multi_rt, non_interactive=True) == "AIzaSyFirstKey00000001"

        r_dup = pipeline.add_api_key_flow(multi_rt, non_interactive=False, json_mode=True)
        assert r_dup == {"slot": 1, "duplicate": True, "verified": True, "total_keys": 2}, r_dup
        assert not (multi_rt / "api_key_3.dpapi").exists()

        failed = False
        try:
            pipeline.add_api_key_flow(multi_rt, non_interactive=False, json_mode=True)
        except pipeline._CLIError as exc:
            failed = exc.code == "API_KEY_TEST_FAILED"
        assert failed, "Invalid key must fail test and not be saved"
        assert not (multi_rt / "api_key_3.dpapi").exists()
    finally:
        pipeline._prompt_api_key_interactive = orig_prompt
        pipeline.test_api_key = orig_test
        if orig_env3 is not None:
            os.environ["GEMINI_API_KEY"] = orig_env3

    # 2h. ClientPool key-first rotation, sticky selection, model fallback & file re-upload
    calls = []
    uploaded_by = []
    deleted_by = []

    class _FakeFile:
        def __init__(self, owner, name):
            self.owner = owner
            self.name = name
            self.state = None

    class _FakeFiles:
        def __init__(self, owner):
            self.owner = owner

        def upload(self, file, config=None):
            uploaded_by.append(self.owner)
            return _FakeFile(self.owner, f"remote_{self.owner}")

        def delete(self, name):
            deleted_by.append((self.owner, name))

    class _FakeModels:
        def __init__(self, owner, fail_set):
            self.owner = owner
            self.fail_set = fail_set

        def generate_content(self, model, contents, config=None):
            calls.append((self.owner, model))
            if (self.owner, model) in self.fail_set:
                raise RuntimeError("429 RESOURCE_EXHAUSTED RequestsPerDay exceeded")
            return type("Resp", (), {"text": f"ok:{self.owner}:{model}"})()

    class _FakeClient:
        def __init__(self, owner, fail_set):
            self.files = _FakeFiles(owner)
            self.models = _FakeModels(owner, fail_set)

    fail_map = {("API1", "gemini-4-hydrogen")}
    pool = pipeline.ClientPool(
        [(1, "AIzaSyKeyOne000000001"), (2, "AIzaSyKeyTwo000000002")],
        client_factory=lambda k: _FakeClient("API1" if "One" in k else "API2", fail_map),
    )
    tmp_f = mig_tmp / "chunk.mp3"
    tmp_f.write_bytes(b"fake")
    resp1, mod1 = pipeline.generate_with_fallback(
        pool,
        ["prompt"],
        None,
        "Test",
        on_status=lambda m: None,
        file_paths=[tmp_f],
        preuploaded=(0, [_FakeFile("API1", "pre_1")]),
    )
    assert mod1 == "gemini-4-hydrogen"
    assert calls == [("API1", "gemini-4-hydrogen"), ("API2", "gemini-4-hydrogen")], calls
    assert pool.current_idx == 1 and uploaded_by == ["API2"] and deleted_by == [("API2", "remote_API2")]

    # Next call sticks to API2 on gemini-4-hydrogen
    calls.clear()
    resp2, mod2 = pipeline.generate_with_fallback(pool, "p2", None, "Test2", on_status=lambda m: None)
    assert mod2 == "gemini-4-hydrogen" and calls == [("API2", "gemini-4-hydrogen")], calls

    # When API2 also exhausts gemini-4-hydrogen, fall back to gemini-3.8-flash
    calls.clear()
    fail_map.add(("API2", "gemini-4-hydrogen"))
    resp3, mod3 = pipeline.generate_with_fallback(pool, "p3", None, "Test3", on_status=lambda m: None)
    assert mod3 == "gemini-3.8-flash" and calls == [("API2", "gemini-4-hydrogen"), ("API2", "gemini-3.8-flash")], calls

    # 2i. v1.7.0: questionary available, TRANSCRIPCION_/APUNTES_ base detection, mode prompts & hardened cleanup
    import questionary
    import stat as _stat
    assert hasattr(questionary, "select")
    p_tx_base = bn_dir / "TRANSCRIPCION_clase1.pdf"
    p_tx_base.write_bytes(b"%PDF-1.4")
    assert pipeline.is_base_notes_pdf(p_tx_base) is True
    assert pipeline.resolve_output_mode("apuntes", non_interactive=True) == "apuntes"
    assert pipeline.resolve_output_mode(None, non_interactive=True) == "transcripcion"

    captured_syn = []
    orig_gwf = pipeline.generate_with_fallback
    try:
        def _fake_gwf(pool_arg, contents_arg, cfg_arg, lbl_arg, on_status=None, file_paths=None, preuploaded=None):
            captured_syn.append((contents_arg, file_paths))
            return type("Resp", (), {"text": "# Titulo\nOk"})(), "gemini-4-hydrogen"

        pipeline.generate_with_fallback = _fake_gwf
        ui_syn = pipeline.PipelineUI(err_rec)
        ui_syn.add_row("review", "Revision")
        p_g1 = bn_dir / "guia1.pdf"
        p_g2 = bn_dir / "guia2.pdf"
        p_g1.write_bytes(b"%PDF-1.4")
        p_g2.write_bytes(b"%PDF-1.4")

        pipeline.synthesize_notes(pool, ["hola"], [], "Tema", ui_syn, mode="transcripcion", guide_docs=[p_g1])
        assert captured_syn[-1][1] is None, "Transcripcion mode must not re-use guide docs"

        pipeline.synthesize_notes(pool, ["hola"], [], "Tema", ui_syn, mode="apuntes", guide_docs=[p_g1])
        assert captured_syn[-1][1] == [p_g1] and "FUSION CON DOCUMENTO GUIA" in captured_syn[-1][0]

        pipeline.synthesize_notes(pool, ["hola"], [], "Tema", ui_syn, mode="apuntes", guide_docs=[p_g1, p_g2])
        assert captured_syn[-1][1] == [p_g1, p_g2] and "MISMA LINEA QUE DOCUMENTOS GUIA" in captured_syn[-1][0]
    finally:
        pipeline.generate_with_fallback = orig_gwf

    ro_dir = mig_tmp / "ro_pipe"
    (ro_dir / "clean").mkdir(parents=True, exist_ok=True)
    ro_file = ro_dir / "clean" / "locked.mp3"
    ro_file.write_bytes(b"123")
    os.chmod(ro_file, _stat.S_IREAD)
    pipeline.cleanup_intermediate(ro_dir)
    assert not ro_dir.exists(), "Hardened cleanup_intermediate must remove read-only temp files and empty folder"

    # 2j. _upload_local_file avoids UnicodeEncodeError on filenames with accents/ñ
    from google.genai import _extra_utils
    acc_pdf = bn_dir / "APUNTES_Política_Criminal.pdf"
    acc_pdf.write_bytes(b"%PDF-1.4")

    class _HeaderCheckFiles:
        def upload(self, *, file, config=None):
            http_opts, sz, mt = _extra_utils.prepare_resumable_upload(
                file, user_mime_type=(config or {}).get("mime_type")
            )
            for hk, hv in (http_opts.headers or {}).items():
                hk.encode("ascii")
                hv.encode("ascii")
            assert mt == "application/pdf" and sz == 8
            return _FakeFile("U", "ok")

    _hdr_client = type("HdrClient", (), {"files": _HeaderCheckFiles()})()
    assert pipeline._upload_local_file(_hdr_client, acc_pdf).name == "ok"

    # 2k. v1.8.0: pypdf slicing, parse_page_ranges, chunked vocab (no 4-hydrogen), multi-PDF filter & large-PDF slice
    import pypdf
    from pypdf import PdfWriter
    assert pipeline.parse_page_ranges([[5, 10], [8, 12], [99, 120], [1, 2]], max_page=20) == [(1, 2), (5, 12), (20, 20)]

    def _make_text_pdf(dest: Path, lines_per_page: list[str]):
        w = PdfWriter()
        for idx, txt in enumerate(lines_per_page, 1):
            page = w.add_blank_page(width=400, height=400)
            stream = pypdf.generic.DecodedStreamObject()
            safe = txt.replace("(", "").replace(")", "")
            stream.set_data(f"BT /F1 12 Tf 40 350 Td ({safe}) Tj ET".encode("latin1"))
            font = pypdf.generic.DictionaryObject({
                pypdf.generic.NameObject("/Type"): pypdf.generic.NameObject("/Font"),
                pypdf.generic.NameObject("/Subtype"): pypdf.generic.NameObject("/Type1"),
                pypdf.generic.NameObject("/BaseFont"): pypdf.generic.NameObject("/Helvetica"),
            })
            res = pypdf.generic.DictionaryObject({
                pypdf.generic.NameObject("/Font"): pypdf.generic.DictionaryObject({
                    pypdf.generic.NameObject("/F1"): font,
                })
            })
            page[pypdf.generic.NameObject("/Resources")] = res
            page[pypdf.generic.NameObject("/Contents")] = stream
        with dest.open("wb") as fh:
            w.write(fh)

    big_pdf = bn_dir / "manual_30p.pdf"
    _make_text_pdf(big_pdf, [f"Tema {(i - 1) // 10 + 1} pagina {i} contenido juridico extenso de prueba" for i in range(1, 31)])
    pgs30 = pipeline.inspect_pdf_pages(big_pdf)
    assert len(pgs30) == 30 and "pagina 11" in pgs30[10]

    sliced_test = bn_dir / "sliced_5p.pdf"
    pipeline.slice_pdf_pages(big_pdf, [(11, 15)], sliced_test)
    pgs5 = pipeline.inspect_pdf_pages(sliced_test)
    assert len(pgs5) == 5 and "pagina 11" in pgs5[0] and "pagina 15" in pgs5[4]

    orig_gwf2 = pipeline.generate_with_fallback
    vocab_pipe = mig_tmp / "vocab_pipe"
    vocab_pipe.mkdir(parents=True, exist_ok=True)
    try:
        vocab_chains = []
        def _fake_gwf_v18(pool_arg, contents_arg, cfg_arg, lbl_arg, on_status=None, file_paths=None, preuploaded=None, models_chain=pipeline.MODELS_CHAIN):
            vocab_chains.append((lbl_arg, models_chain))
            if lbl_arg == "Apuntes":
                return type("Resp", (), {"text": '["terminoA", "terminoB"]'})(), models_chain[0]
            if lbl_arg == "Triaje":
                return type("Resp", (), {"text": '{"relevant": ["tema2.pdf"]}'})(), models_chain[0]
            return type("Resp", (), {"text": '{"ranges": [[11, 15]]}'})(), models_chain[0]

        pipeline.generate_with_fallback = _fake_gwf_v18
        ui_t = pipeline.PipelineUI(err_rec)
        ui_t.add_row("vocab", "Apuntes")
        terms_v18, v_mod = pipeline.extract_vocabulary(pool, [big_pdf], vocab_pipe, ui_t)
        assert terms_v18 == ["terminoA", "terminoB"] and v_mod == "gemini-3.8-flash"
        assert len([c for l, c in vocab_chains if l == "Apuntes"]) == 1
        assert all("gemini-4-hydrogen" not in c for _, c in vocab_chains)

        # Large >60k-char text PDF splits into 2+ vocab batches
        vocab_chains.clear()
        huge_pdf = bn_dir / "manual_huge.pdf"
        _make_text_pdf(huge_pdf, [("Contenido extenso juridico de prueba " * 80) for _ in range(1, 31)])
        pipeline.extract_vocabulary(pool, [huge_pdf], vocab_pipe, ui_t)
        assert len([c for l, c in vocab_chains if l == "Apuntes"]) >= 2
        assert all("gemini-4-hydrogen" not in c for _, c in vocab_chains)

        t1 = bn_dir / "tema1.pdf"
        t2 = bn_dir / "tema2.pdf"
        t3 = bn_dir / "tema3.pdf"
        for idx_t, tp in enumerate((t1, t2, t3), 1):
            _make_text_pdf(tp, [f"Documento tema {idx_t} contenido completo de clase"])
        ui_t.add_row("triage", "Filtrando apuntes")
        sel3, info3, _ = pipeline.select_and_slice_docs(pool, [t1, t2, t3], ["Hoy vimos tema 2"], vocab_pipe, ui_t)
        assert sel3 == [t2] and "1/3" in info3, (sel3, info3)

        sel_big, info_big, _ = pipeline.select_and_slice_docs(pool, [big_pdf], ["Hoy vimos tema 2"], vocab_pipe, ui_t)
        assert len(sel_big) == 1 and len(pipeline.inspect_pdf_pages(sel_big[0])) == 5 and "5/30" in info_big
        pipeline.cleanup_intermediate(vocab_pipe)
        assert not vocab_pipe.exists()

        # 2l. v1.8.1: plan_large_pdf_topics multi-topic split + new-topic style_only + multi-PDF render_summary
        split_pipe = mig_tmp / "split_pipe"
        split_pipe.mkdir(parents=True, exist_ok=True)
        def _fake_gwf_split(pool_arg, contents_arg, cfg_arg, lbl_arg, on_status=None, file_paths=None, preuploaded=None, models_chain=pipeline.MODELS_CHAIN):
            return type("Resp", (), {"text": '{"topics": [{"title": "Tema 1", "ranges": [[1, 10]]}, {"title": "Tema 2", "ranges": [[11, 20]]}]}'})(), models_chain[0]

        pipeline.generate_with_fallback = _fake_gwf_split
        jobs2, info2, _ = pipeline.plan_large_pdf_topics(pool, big_pdf, ["Temas 1 y 2"], split_pipe, ui_t)
        assert len(jobs2) == 2 and all(not j["style_only"] and j["pages"] == 10 for j in jobs2) and "2 tema(s)" in info2

        def _fake_gwf_style(pool_arg, contents_arg, cfg_arg, lbl_arg, on_status=None, file_paths=None, preuploaded=None, models_chain=pipeline.MODELS_CHAIN):
            return type("Resp", (), {"text": '{"topics": []}'})(), models_chain[0]

        pipeline.generate_with_fallback = _fake_gwf_style
        jobs_st, info_st, _ = pipeline.plan_large_pdf_topics(pool, big_pdf, ["Tema nuevo"], split_pipe, ui_t)
        assert len(jobs_st) == 1 and jobs_st[0]["style_only"] is True and jobs_st[0]["pages"] == 5 and "nuevo tema" in info_st

        syn_prompts = []
        pipeline.generate_with_fallback = lambda *a, **kw: (syn_prompts.append(a[1]) or type("Resp", (), {"text": "# Nuevo"})(), "gemini-4-hydrogen")
        pipeline.synthesize_notes(pool, ["texto nuevo"], [], "Nuevo", ui_t, mode="apuntes", guide_docs=[jobs_st[0]["path"]], style_only=True)
        assert "MODO REFERENCIA DE ESTILO SOLAMENTE" in syn_prompts[-1]

        pipeline.render_summary([t1, t2], [t1], [t1], [], [], 12.0, target_console=bn_rec, mode="apuntes")
        pipeline.cleanup_intermediate(split_pipe)
        assert not split_pipe.exists()

        # 2m. v1.8.2: RemoteProtocolError / socket drop classified as transient and retried
        class RemoteProtocolError(Exception):
            pass

        err_disc = RemoteProtocolError("Server disconnected without sending a response.")
        assert pipeline._classify_error(err_disc) == "transient"
        attempts = [0]
        def _flaky_call():
            attempts[0] += 1
            if attempts[0] == 1:
                raise err_disc
            return "recovered"
        assert pipeline.call_with_retry(_flaky_call, "NetTest", delays=(0,)) == "recovered"
        assert attempts[0] == 2

        # 2n. v1.8.3: _transient_reason distinguishes 429 daily, 429 RPM/TPM, 503 and network drops
        assert "429 cuota diaria" in pipeline._transient_reason(RuntimeError("429 RequestsPerDay"))
        assert "429 limite" in pipeline._transient_reason(RuntimeError("429 RESOURCE_EXHAUSTED"))
        assert "503" in pipeline._transient_reason(RuntimeError("503 UNAVAILABLE high demand"))
        assert "corte de red" in pipeline._transient_reason(err_disc)
    finally:
        pipeline.generate_with_fallback = orig_gwf2
finally:
    shutil.rmtree(mig_tmp, ignore_errors=True)

# 3. Real _process_one_audio smoke test with ffmpeg -progress pipe:1
tmp = Path(r"C:\Users\pablo\.gemini\antigravity\brain\cac239b5-6525-4158-9902-758e46192b38\scratch\_smoke_v13")
(tmp / "clean").mkdir(parents=True, exist_ok=True)
(tmp / "chunks").mkdir(exist_ok=True)
try:
    ui = pipeline.PipelineUI(pipeline.console)
    ui.add_row("audio_1", "frag1.wav")
    chunks, dur = pipeline._process_one_audio(
        1,
        Path(r"C:\Users\pablo\.gemini\antigravity\brain\cac239b5-6525-4158-9902-758e46192b38\scratch\frag1.wav"),
        tmp / "clean",
        tmp / "chunks",
        ui,
    )
    assert len(chunks) == 1 and chunks[0].stat().st_size > 10000 and dur > 10.0
    sz = chunks[0].stat().st_size
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# 4. Warm startup time of procesar_clase.bat --version
bat = str(REPO_ROOT / "procesar_clase.bat")
subprocess.run(["cmd", "/c", bat, "--version"], capture_output=True, check=True, env={**os.environ, "CLASE2PDF_NO_UPDATE": "1"})  # warm cache
t0 = time.perf_counter()
subprocess.run(["cmd", "/c", bat, "--version"], capture_output=True, check=True, env={**os.environ, "CLASE2PDF_NO_UPDATE": "1"})
warm_ms = (time.perf_counter() - t0) * 1000.0

print(f"V13_VERIFY_OK mp3={sz}B dur={dur:.1f}s warm_bat_ms={warm_ms:.0f}")

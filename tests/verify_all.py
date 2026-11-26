import os
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import pipeline

# 1. strip_markdown_fences
fenced = "```markdown\n# Titulo\nHola\n```"
assert pipeline.strip_markdown_fences(fenced) == "# Titulo\nHola"

# 2. md_to_html (####, 1., >, table, nested parens in nota)
md = """#### Subtitulo
1. Uno
2. Dos
> Cita importante
| Col1 | Col2 |
| --- | --- |
| A | B |
Texto (nota: ver X (1953)) fin."""
html_out = pipeline.md_to_html(md)
assert "<h4>Subtitulo</h4>" in html_out
assert "<ol><li>Uno</li>" in html_out or "<ol>\n<li>Uno</li>" in html_out
assert "<blockquote>Cita importante</blockquote>" in html_out
assert "<table><thead><tr><th>Col1</th><th>Col2</th></tr></thead>" in html_out
assert '<span class="note">(nota: ver X (1953))</span>' in html_out

# 3. read_docx_text
tmp_docx = REPO_ROOT / "tests" / "_test.docx"
try:
    with zipfile.ZipFile(tmp_docx, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Hola </w:t></w:r><w:r><w:t>mundo</w:t></w:r></w:p></w:body>"
            "</w:document>",
        )
    assert pipeline.read_docx_text(tmp_docx) == "Hola mundo"
finally:
    tmp_docx.unlink(missing_ok=True)

# 4. unique_output_path
bat_path = REPO_ROOT / "procesar_clase.bat"
u = pipeline.unique_output_path(bat_path)
assert u.name == "procesar_clase_2.bat"

# 5. Thin self-updating .bat checks
bat_text = bat_path.read_text(encoding="ascii")
assert "!PY_CMD!" in bat_text and "!errorlevel!" in bat_text
assert "RUNTIME_DIR" in bat_text and "LOCALAPPDATA" in bat_text
assert "manifest.json" in bat_text and "CLASE2PDF_REPO_URL" in bat_text
assert ".clase_pipeline" not in bat_text
assert len(bat_text.splitlines()) < 200

print("ALL_CHECKS_OK")

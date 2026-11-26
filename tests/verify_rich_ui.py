import hashlib
import json
import sys
from pathlib import Path
from rich.console import Console

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import pipeline

rec = Console(record=True, width=100, theme=pipeline.THEME)
fake_audio = REPO_ROOT / "procesar_clase.bat"
fake_doc = REPO_ROOT / "pipeline.py"

pipeline.render_intro(fake_audio.parent, [fake_audio], [fake_doc], 2, target_console=rec)

ui = pipeline.PipelineUI(rec)
ui.add_row("vocab", "Apuntes")
ui.add_row("audio_1", pipeline.escape("[Clase 1].mp4"))
ui.update_row("vocab", "[ok]✓ Listo[/]", "42 terminos")
ui.update_row("audio_1", "[ok]✓ Listo[/]", "32:17")
ui.set_progress(description="Terminado", total=1, completed=1)
rec.print(ui.render())

pipeline.render_summary(
    fake_doc, [fake_audio], [fake_audio], ["a", "b"], ["gemini-4-hydrogen"], 65.2, target_console=rec
)

out = rec.export_text()
assert "Clase2PDF" in out
assert "42 terminos" in out
assert "✓ Listo" in out
assert "FFmpeg" not in out
assert "Hydrogen" not in out
assert "Cascada" not in out

manifest = json.loads((REPO_ROOT / "manifest.json").read_text(encoding="utf-8"))
assert manifest["version"] == pipeline.APP_VERSION
assert manifest["pipeline_sha256"] == hashlib.sha256(fake_doc.read_bytes()).hexdigest().lower()
assert manifest["bat_sha256"] == hashlib.sha256(fake_audio.read_bytes()).hexdigest().lower()

print("RICH_AND_SHA_OK")

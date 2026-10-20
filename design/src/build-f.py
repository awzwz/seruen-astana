"""Builds design/mockup-f.html from the template and the real data (design/src/f-data.json)."""
import json
from pathlib import Path
here = Path(__file__).parent
html = (here / "f-template.html").read_text(encoding="utf-8")
data = json.loads((here / "f-data.json").read_text(encoding="utf-8"))
(here.parent / "mockup-f.html").write_text(html.replace("/*__DATA__*/", json.dumps(data, ensure_ascii=False)), encoding="utf-8")
print("mockup-f.html written")

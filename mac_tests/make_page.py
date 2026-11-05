"""Builds output/dashboard.html for the tests from a made-up student, with no Blackboard or AI."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main
main.DATA_DIR.mkdir(parents=True, exist_ok=True)
main.SNAPSHOT.write_text(json.dumps({"synced": main.now().isoformat(), "me": {"name": "Sam Tester"},
                                     "courses": [], "calendar": []}), encoding="utf-8")
page = Path(main.build_lite())
# no Claude key here, so the page comes out locked; the tests want the unlocked app
page.write_text(page.read_text(encoding="utf-8").replace('<html lang="en" class="locked">', '<html lang="en">', 1), encoding="utf-8")
print(page)

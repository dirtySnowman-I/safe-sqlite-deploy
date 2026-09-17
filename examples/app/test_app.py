import os
import sqlite3
from pathlib import Path
assert Path("build.txt").is_file()
assert "DEMO_PRODUCTION_SECRET" not in os.environ
with sqlite3.connect(os.environ["SQLITE_PATH"]) as db:
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.execute("SELECT id, text FROM notes LIMIT 1").fetchall()
print("Candidate unit/database checks passed")

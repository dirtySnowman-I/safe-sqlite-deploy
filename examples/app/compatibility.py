import json
import os
import sqlite3
import urllib.request
import uuid

base = os.environ["BASE_URL"]
text = "compatibility-" + uuid.uuid4().hex
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
request = urllib.request.Request(base + "/notes", data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
with opener.open(request, timeout=2) as response:
    note_id = json.load(response)["id"]
with opener.open(base + "/notes", timeout=2) as response:
    assert any(note["id"] == note_id and note["text"] == text for note in json.load(response))
# This hook is run ONLY against isolated candidate/compatibility databases.
with sqlite3.connect(os.environ["SQLITE_PATH"]) as db:
    assert db.execute("SELECT text FROM notes WHERE id=?", (note_id,)).fetchone()[0] == text
    db.execute("DELETE FROM notes WHERE id=?", (note_id,))
print("Runtime read/write compatibility passed")

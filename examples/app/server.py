import json
import os
from pathlib import Path
import signal
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATABASE = os.environ["SQLITE_PATH"]
REVISION = Path("DEPLOYED_REVISION").read_text().strip()
INSTANCE = os.environ["DEPLOYMENT_INSTANCE"]

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            with sqlite3.connect(DATABASE) as db:
                if self.path == "/health":
                    db.execute("SELECT id, text FROM notes LIMIT 1").fetchall()
                    if os.environ.get("DEPLOY_MODE") == "production" and Path("FAIL_PRODUCTION").exists():
                        return self.reply(503, {"status": "injected-failure"})
                    return self.reply(200, {"status": "ok", "database": "ok", "revision": REVISION, "instance": INSTANCE})
                if self.path == "/notes":
                    return self.reply(200, [{"id": i, "text": text} for i, text in db.execute("SELECT id,text FROM notes ORDER BY id")])
            self.reply(404, {"error": "not_found"})
        except (sqlite3.Error, OSError):
            self.reply(503, {"error": "database_unavailable"})

    def do_POST(self):
        if self.path != "/notes":
            return self.reply(404, {"error": "not_found"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                return self.reply(413, {"error": "body_limit"})
            text = json.loads(self.rfile.read(size)).get("text")
            if not isinstance(text, str) or not text or len(text) > 1000:
                return self.reply(400, {"error": "invalid_text"})
            with sqlite3.connect(DATABASE) as db:
                row = db.execute("INSERT INTO notes(text) VALUES(?)", (text,))
                return self.reply(201, {"id": row.lastrowid})
        except (ValueError, sqlite3.Error):
            self.reply(400, {"error": "invalid_request"})

server = ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler)
server.daemon_threads = True
signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))
try:
    server.serve_forever(poll_interval=0.05)
finally:
    server.server_close()

import json
import socket
import time
import urllib.request
from .util import require, DeployError

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_health(url, revision, instance, timeout, process=None):
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise DeployError("CANDIDATE_EXITED", "Candidate process exited before passing health.")
        try:
            request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
            with opener.open(request, timeout=min(1, max(0.05, deadline-time.monotonic()))) as response:
                raw = response.read(65537)
                require(len(raw) <= 65536, "HEALTH_RESPONSE_LIMIT", "Health response too large.")
                data = json.loads(raw)
                if response.status == 200 and isinstance(data, dict) and data.get("status") == "ok" and data.get("database") == "ok" and data.get("revision") == revision and data.get("instance") == instance:
                    return data
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise DeployError("HEALTH_FAILED", "Health did not confirm the expected revision, instance, and database.")

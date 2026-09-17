"""Linux demo service adapter. Production should use a supervised service adapter."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

root = Path(os.environ["DEPLOY_STATE"])
root.mkdir(parents=True, exist_ok=True)
pidfile = root / "service.json"

def identity(pid):
    try:
        # Field 22 is starttime; comm may contain spaces/parentheses.
        fields = Path("/proc/" + str(pid) + "/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0], fields[19]
    except FileNotFoundError:
        return None

def stop():
    if not pidfile.exists():
        return
    record = json.loads(pidfile.read_text())
    current = identity(record["pid"])
    if current is None or current[0] == "Z":
        pidfile.unlink(missing_ok=True)
        return
    if current[1] != record["start"]:
        raise RuntimeError("PID was reused; refusing to signal an unrelated process")
    os.killpg(record["pid"], signal.SIGTERM)
    for _ in range(100):
        current = identity(record["pid"])
        if current is None or current[0] == "Z":
            pidfile.unlink(missing_ok=True)
            return
        time.sleep(0.02)
    if identity(record["pid"])[1] != record["start"]:
        raise RuntimeError("PID changed while stopping")
    os.killpg(record["pid"], signal.SIGKILL)
    for _ in range(100):
        current = identity(record["pid"])
        if current is None or current[0] == "Z":
            pidfile.unlink(missing_ok=True)
            return
        time.sleep(0.02)
    raise RuntimeError("Service did not stop")

if sys.argv[1] == "stop":
    stop()
elif sys.argv[1] == "start":
    if pidfile.exists():
        raise RuntimeError("Stop the previous service first")
    release = Path(os.environ["RELEASE_DIR"])
    manifest = json.loads((release / "release.json").read_text())
    with open(root / "service.log", "ab") as output:
        os.chmod(root / "service.log", 0o600)
        proc = subprocess.Popen(manifest["start"], cwd=release, env=os.environ.copy(), stdin=subprocess.DEVNULL,
                                stdout=output, stderr=output, start_new_session=True)
    current = identity(proc.pid)
    if not current:
        raise RuntimeError("Service exited immediately")
    temp = pidfile.with_suffix(".tmp")
    temp.write_text(json.dumps({"pid": proc.pid, "start": current[1]}))
    os.chmod(temp, 0o600)
    os.replace(temp, pidfile)
else:
    raise RuntimeError("Expected start or stop")

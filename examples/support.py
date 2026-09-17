from pathlib import Path
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from safe_sqlite_deploy.config import load_config
from safe_sqlite_deploy.deployer import Deployer
from safe_sqlite_deploy.health import free_port

ROOT = Path(__file__).resolve().parent

class DemoProject:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.repo = self.directory / "source"
        shutil.copytree(ROOT / "app", self.repo, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Deployment Demo")
        self.git("config", "user.email", "demo@example.invalid")
        self.revision = self.commit("Initial demo application")
        self.database = self.directory / "live.sqlite"
        sqlite3.connect(self.database).close()
        os.chmod(self.database, 0o600)
        self.port = free_port()
        self.base = "http://127.0.0.1:" + str(self.port)
        self.state = self.directory / "state"
        self.secret = self.directory / "production.json"
        self.secret.write_text(json.dumps({"DEMO_PRODUCTION_SECRET": "local-demo-value"}))
        os.chmod(self.secret, 0o600)
        self.config_path = self.directory / "deploy.json"
        self.config_path.write_text(json.dumps({
            "repository": str(self.repo), "database": str(self.database), "stateDir": str(self.state),
            "productionEnvFile": str(self.secret), "commandTimeoutSeconds": 10, "healthTimeoutSeconds": 1.5,
            "service": {"start": [sys.executable, str(ROOT / "service.py"), "start"],
                        "stop": [sys.executable, str(ROOT / "service.py"), "stop"],
                        "healthUrl": self.base + "/health", "port": self.port}}))
        self.deployer = Deployer(load_config(self.config_path))

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL).decode().strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def get(self, path):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self.base + path, timeout=2) as response:
            return json.load(response)

    def note(self, text):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(self.base + "/notes", data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=2) as response:
            return json.load(response)

    def stop(self):
        env = {**os.environ, "DEPLOY_STATE": str(self.state)}
        subprocess.run([sys.executable, str(ROOT / "service.py"), "stop"], env=env, check=True, timeout=10)

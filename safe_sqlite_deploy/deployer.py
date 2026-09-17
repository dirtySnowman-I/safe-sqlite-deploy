from __future__ import annotations
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
from .config import load_manifest
from .database import backup_database, migrate, plan_migrations, migration_files
from .health import free_port, wait_health
from .source import inspect_source, export_source
from .util import DeployError, require, atomic_json, read_json, deployment_lock, clean_env, run_command, contained, stop_process


class Deployer:
    def __init__(self, config):
        self.config = config
        self.root = config["stateDir"]
        self.repo = config["repository"]
        self.database = config["database"]
        self.journal_path = self.root / "journal.json"
        self.journal = None

    def current(self):
        pointer = self.root / "current"
        if not pointer.exists() and not pointer.is_symlink():
            return None
        require(pointer.is_symlink(), "INVALID_CURRENT", "current must be a managed symlink.")
        path = pointer.resolve()
        require(path.is_dir() and path.parent == (self.root / "releases").resolve(), "INVALID_CURRENT", "current must point to a managed release.")
        return path

    def revision(self, release):
        value = (release / "DEPLOYED_REVISION").read_text().strip()
        require(len(value) in {40, 64} and all(c in "0123456789abcdef" for c in value), "INVALID_RELEASE", "Release revision marker is invalid.")
        return value

    def phase(self, value, **fields):
        self.journal.update(phase=value, **fields)
        atomic_json(self.journal_path, self.journal)

    def finish(self, status):
        self.phase(status, finishedAt=time.time())
        atomic_json(self.root / "reports" / (self.journal["id"] + ".json"), self.journal)
        self.journal_path.unlink(missing_ok=True)

    def runtime_env(self, release, database, instance, port, production=False):
        extra = {}
        if production and self.config.get("productionEnvFile"):
            path = self.config["productionEnvFile"]
            require(path.is_file() and path.stat().st_mode & 0o077 == 0, "PRIVATE_ENV_REQUIRED", "Production environment JSON must have mode 600 or stricter.")
            extra = read_json(path)
            reserved = {"PATH", "HOME", "PORT", "SQLITE_PATH", "RELEASE_REVISION", "DEPLOYMENT_INSTANCE", "RELEASE_DIR", "DEPLOY_STATE", "DEPLOY_MODE", "BASE_URL"}
            require(isinstance(extra, dict) and not (set(extra) & reserved) and all(isinstance(k, str) and isinstance(v, str) for k, v in extra.items()), "INVALID_ENV", "Production environment must be a string map without reserved deployment keys.")
        extra.update({"PORT": port, "SQLITE_PATH": database, "RELEASE_REVISION": self.revision(release),
                      "DEPLOYMENT_INSTANCE": instance, "RELEASE_DIR": release, "DEPLOY_STATE": self.root,
                      "DEPLOY_MODE": "production" if production else "candidate"})
        return clean_env(self.root / "home", extra)

    def command(self, argv, release, env, label):
        run_command(argv, cwd=release, env=env, timeout=self.config["commandTimeoutSeconds"],
                    log=self.root / "logs" / (self.journal["id"] + "-" + label + ".log"))

    @contextlib.contextmanager
    def candidate(self, release, database, label):
        manifest = load_manifest(release)
        port, instance = free_port(), uuid.uuid4().hex
        env = self.runtime_env(release, database, instance, port)
        url = "http://127.0.0.1:" + str(port)
        env["BASE_URL"] = url
        log = self.root / "logs" / (self.journal["id"] + "-" + label + ".log")
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(log, "ab") as out:
            os.chmod(log, 0o600)
            process = subprocess.Popen(manifest["start"], cwd=release, env=env, stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=out, start_new_session=True)
            try:
                wait_health(url + manifest["healthPath"], self.revision(release), instance,
                            self.config["healthTimeoutSeconds"], process)
                yield env
            finally:
                stop_process(process)

    def switch_current(self, release):
        pointer = self.root / "current"
        if release is None:
            pointer.unlink(missing_ok=True)
            return
        temp = self.root / ("current-" + uuid.uuid4().hex)
        try:
            temp.symlink_to(Path("releases") / release.name)
            os.replace(temp, pointer)
        finally:
            temp.unlink(missing_ok=True)

    def service(self, action, release, instance):
        env = self.runtime_env(release, self.database, instance, self.config["service"]["port"], production=True)
        substitutions = {"state": str(self.root), "release": str(release), "database": str(self.database),
                         "revision": self.revision(release), "instance": instance, "port": str(self.config["service"]["port"])}
        try:
            argv = [part.format_map(substitutions) for part in self.config["service"][action]]
        except (KeyError, ValueError) as exc:
            raise DeployError("INVALID_COMMAND", "Unknown service command placeholder.") from exc
        self.command(argv, release, env, "service-" + action)

    def rollback(self):
        candidate = self.root / "releases" / self.journal["candidate"]
        previous = self.root / "releases" / self.journal["previous"] if self.journal["previous"] else None
        require(candidate.parent.resolve() == (self.root / "releases").resolve() and
                (previous is None or previous.parent.resolve() == (self.root / "releases").resolve()),
                "INVALID_JOURNAL", "Invalid release location in journal.")
        self.phase("rolling_back")
        self.service("stop", candidate, self.journal["instance"])
        self.switch_current(previous)
        if previous:
            instance = uuid.uuid4().hex
            self.service("start", previous, instance)
            wait_health(self.config["service"]["healthUrl"], self.revision(previous), instance, self.config["healthTimeoutSeconds"])
            atomic_json(self.root / "active.json", {"release": previous.name, "revision": self.revision(previous), "instance": instance})
        else:
            (self.root / "active.json").unlink(missing_ok=True)
        # Never restore an old database automatically. New writes must survive runtime rollback.
        self.finish("rolled_back")

    def check(self, ref="HEAD"):
        revision = inspect_source(self.repo, ref)
        require(self.database.is_file() and not self.database.is_symlink(), "INVALID_DATABASE", "Live database must already exist.")
        return {"revision": revision, "current": self.revision(self.current()) if self.current() else None,
                "requiresRecovery": self.journal_path.exists()}

    def deploy(self, ref="HEAD"):
        with deployment_lock(self.root):
            require(not self.journal_path.exists(), "RECOVERY_REQUIRED", "An interrupted deployment exists. Run recover before deploying.")
            checked = self.check(ref)
            previous = self.current()
            if checked["current"] == checked["revision"]:
                return {"status": "unchanged", "revision": checked["revision"]}
            run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
            release = self.root / "releases" / run_id
            work = self.root / "work" / run_id
            work.mkdir(parents=True, mode=0o700)
            self.journal = {"id": run_id, "phase": "preparing", "candidate": run_id,
                            "previous": previous.name if previous else None, "revision": checked["revision"],
                            "instance": uuid.uuid4().hex, "startedAt": time.time()}
            atomic_json(self.journal_path, self.journal)
            activating = False
            try:
                export_source(self.repo, checked["revision"], release)
                manifest = load_manifest(release)
                migrations = contained(release, manifest["migrations"])
                original_migrations = [(name, digest) for name, digest, _ in migration_files(migrations)]
                snapshot = work / "candidate.sqlite"
                backup_database(self.database, snapshot)
                pending = migrate(snapshot, migrations)
                env = self.runtime_env(release, snapshot, uuid.uuid4().hex, free_port())
                for i, argv in enumerate(manifest["build"]):
                    self.command(argv, release, env, "build-" + str(i))
                self.command(manifest["test"], release, env, "tests")
                require(load_manifest(release) == manifest and self.revision(release) == checked["revision"] and
                        [(n, d) for n, d, _ in migration_files(migrations)] == original_migrations,
                        "BUILD_MUTATED_CONTRACT", "Build changed the manifest, revision marker, or migrations.")
                with self.candidate(release, snapshot, "candidate") as test_env:
                    self.command(manifest["compatibility"], release, test_env, "candidate-contract")
                if previous:
                    compatible_db = work / "compatibility.sqlite"
                    backup_database(snapshot, compatible_db)
                    old_manifest = load_manifest(previous)
                    with self.candidate(previous, compatible_db, "previous-runtime") as old_env:
                        self.command(old_manifest["compatibility"], previous, old_env, "rollback-contract")
                self.phase("prepared", pendingMigrations=pending)
                # Confirm source cleanliness again before touching the service.
                require(inspect_source(self.repo, ref) == checked["revision"], "SOURCE_CHANGED", "Source ref changed during candidate checks.")
                self.phase("activating")
                activating = True
                self.service("stop", previous or release, self.journal["instance"])
                backup = self.root / "backups" / (run_id + ".sqlite")
                digest = backup_database(self.database, backup)
                self.phase("migrating", backup=str(backup), backupSha256=digest)
                require([name for name, _, _ in plan_migrations(self.database, migrations)] == pending,
                        "MIGRATION_RACE", "Live migration history changed during candidate checks.")
                migrate(self.database, migrations)
                self.phase("switching")
                self.switch_current(release)
                self.service("start", release, self.journal["instance"])
                wait_health(self.config["service"]["healthUrl"], checked["revision"], self.journal["instance"], self.config["healthTimeoutSeconds"])
                atomic_json(self.root / "active.json", {"release": release.name, "revision": checked["revision"], "instance": self.journal["instance"]})
                self.finish("completed")
                return {"status": "completed", "revision": checked["revision"], "backup": str(backup), "report": str(self.root / "reports" / (run_id + ".json"))}
            except BaseException as error:
                self.journal["error"] = error.code if isinstance(error, DeployError) else type(error).__name__
                if activating:
                    try:
                        self.rollback()
                    except BaseException as rollback_error:
                        self.phase("recovery_required", rollbackError=type(rollback_error).__name__)
                        raise DeployError("ROLLBACK_FAILED", "Automatic rollback did not pass. Service state is uncertain; inspect journal and run recover.") from rollback_error
                else:
                    self.finish("rejected")
                raise
            finally:
                shutil.rmtree(work, ignore_errors=True)

    def recover(self):
        with deployment_lock(self.root):
            require(self.journal_path.exists(), "NO_RECOVERY", "No interrupted deployment exists.")
            self.journal = read_json(self.journal_path)
            phase = self.journal.get("phase")
            if phase in {"preparing", "prepared", "rejected", "completed", "rolled_back"}:
                self.finish("recovered_without_activation")
            else:
                try:
                    self.rollback()
                except BaseException as error:
                    self.phase("recovery_required", rollbackError=type(error).__name__)
                    raise DeployError("ROLLBACK_FAILED", "Recovery failed; inspect the journal and private logs.") from error
            return {"status": "recovered"}

"""Run with python3 -m examples.demo from the repository root."""
import json
from pathlib import Path
import uuid
from .support import DemoProject
from safe_sqlite_deploy.util import DeployError


def main():
    directory = Path(".demo") / uuid.uuid4().hex
    directory = directory.resolve()
    directory.mkdir(parents=True)
    project = DemoProject(directory)
    try:
        first = project.deployer.deploy()
        assert project.get("/health")["revision"] == first["revision"]
        project.note("Keep this live write")
        print("Initial release healthy; live note created.")
        (project.repo / "migrations/002_archived.sql").write_text("ALTER TABLE notes ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;\n")
        second = project.commit("Add backward-compatible archived column")
        project.deployer.deploy()
        assert project.get("/health")["revision"] == second
        assert project.get("/notes")[0]["text"] == "Keep this live write"
        print("Additive migration deployed; previous runtime passed read/write compatibility.")
        (project.repo / "migrations/003_drop.sql").write_text("DROP TABLE notes;\n")
        project.commit("Deliberately unsafe migration")
        try:
            project.deployer.deploy()
            raise AssertionError("Destructive migration should fail")
        except DeployError as error:
            assert error.code == "UNSAFE_MIGRATION"
        assert project.get("/health")["revision"] == second
        print("Destructive migration rejected before service activation.")
        (project.repo / "migrations/003_drop.sql").unlink()
        (project.repo / "FAIL_PRODUCTION").write_text("Only production health should fail.\n")
        project.commit("Deliberate activation health failure")
        project.note("A newer write must survive rollback")
        try:
            project.deployer.deploy()
            raise AssertionError("Activation should fail")
        except DeployError as error:
            assert error.code == "HEALTH_FAILED", error.code
        assert project.get("/health")["revision"] == second
        assert [n["text"] for n in project.get("/notes")] == ["Keep this live write", "A newer write must survive rollback"]
        print("Activation failed; previous runtime restored; both live writes preserved.")
        print("Demo passed. Backups, reports, and logs: " + str(project.state))
    finally:
        project.stop()

if __name__ == "__main__":
    main()

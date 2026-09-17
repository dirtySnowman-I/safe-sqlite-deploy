from __future__ import annotations
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time


class DeployError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise DeployError(code, message)


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text())


@contextlib.contextmanager
def deployment_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not root.is_symlink(), "UNSAFE_PATH", "State directory must not be a symlink.")
    os.chmod(root, 0o700)
    fd = os.open(root / "deploy.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeployError("DEPLOYMENT_LOCKED", "Another deployment holds the lock.") from exc
        yield
    finally:
        os.close(fd)


def clean_env(home: Path, extra=None):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
              "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    if extra:
        result.update({str(k): str(v) for k, v in extra.items()})
    return result


def stop_process(process, grace=2):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def run_command(argv, *, cwd: Path, env, timeout=120, log: Path | None = None):
    require(isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv),
            "INVALID_COMMAND", "Commands must be nonempty argument arrays.")
    if log:
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (open(log, "ab") if log else tempfile.TemporaryFile()) as output:
        if log:
            os.chmod(log, 0o600)
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=output, start_new_session=True)
        except OSError as exc:
            raise DeployError("COMMAND_UNAVAILABLE", "Could not start a configured command.") from exc
        try:
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                require(time.monotonic() < deadline, "COMMAND_TIMEOUT", "Configured command exceeded its deadline.")
                require(os.fstat(output.fileno()).st_size <= 20 * 1024 * 1024, "LOG_LIMIT", "Command log exceeded 20 MiB.")
                time.sleep(0.02)
        except BaseException:
            stop_process(process)
            raise
        require(process.returncode == 0, "COMMAND_FAILED", "Configured command failed; inspect the private log.")
        require(output.tell() <= 20 * 1024 * 1024, "LOG_LIMIT", "Command log exceeded 20 MiB.")


def contained(root: Path, relative: str):
    require(isinstance(relative, str) and relative and not Path(relative).is_absolute(), "UNSAFE_PATH", "Expected a relative path.")
    candidate = root / relative
    require(candidate.resolve().is_relative_to(root.resolve()), "UNSAFE_PATH", "Path escapes its release directory.")
    return candidate

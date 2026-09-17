from pathlib import Path, PurePosixPath
import io
import os
import re
import subprocess
import tarfile
from .util import DeployError, require, clean_env


def git(repo: Path, *arguments):
    try:
        result = subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True, timeout=30,
                                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"})
        return result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeployError("GIT_FAILED", "Git source check failed.") from exc


def inspect_source(repo: Path, ref: str):
    require(isinstance(ref, str) and ref and not ref.startswith("-") and len(ref) <= 200, "INVALID_REF", "Invalid Git ref.")
    require(git(repo, "rev-parse", "--show-toplevel").decode().strip() == str(repo.resolve()), "INVALID_REPOSITORY", "Use the repository root.")
    require(not git(repo, "status", "--porcelain", "--untracked-files=normal").strip(), "DIRTY_SOURCE", "Commit or remove source changes before deployment.")
    revision = git(repo, "rev-parse", "--verify", ref + "^{commit}").decode().strip()
    require(re.fullmatch(r"[a-f0-9]{40,64}", revision), "INVALID_REF", "Ref must resolve to a commit.")
    files = git(repo, "ls-tree", "-r", "-z", revision).split(b"\0")
    for item in files:
        if not item:
            continue
        header, name = item.split(b"\t", 1)
        mode = header.split()[0]
        require(mode in {b"100644", b"100755"}, "UNSUPPORTED_SOURCE", "Symlinks and submodules are not supported in releases.")
        path = PurePosixPath(name.decode())
        require(not any(part == ".env" or (part.startswith(".env.") and part != ".env.example") for part in path.parts), "TRACKED_ENV", "Do not commit environment-secret files into release sources.")
    return revision


def export_source(repo: Path, revision: str, destination: Path):
    raw = git(repo, "archive", "--format=tar", revision)
    require(len(raw) <= 100 * 1024 * 1024, "SOURCE_LIMIT", "Source archive exceeds 100 MiB.")
    destination.mkdir(parents=True, mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for item in archive.getmembers():
            path = PurePosixPath(item.name)
            require(not path.is_absolute() and ".." not in path.parts and (item.isfile() or item.isdir()), "UNSAFE_ARCHIVE", "Unsupported archive entry.")
            out = destination.joinpath(*path.parts)
            if item.isdir():
                out.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.extractfile(item) as src, open(out, "xb") as dst:
                    dst.write(src.read())
                os.chmod(out, 0o700 if item.mode & 0o111 else 0o600)
    (destination / "DEPLOYED_REVISION").write_text(revision + "\n")
    os.chmod(destination / "DEPLOYED_REVISION", 0o600)

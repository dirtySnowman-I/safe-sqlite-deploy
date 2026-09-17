from pathlib import Path
import json
from urllib.parse import urlparse
from .util import require, DeployError, contained


def command(value):
    require(isinstance(value, list) and value and all(isinstance(v, str) and v for v in value),
            "INVALID_CONFIG", "Each command must be a nonempty list of arguments.")
    return value


def load_config(path):
    path = Path(path).absolute()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise DeployError("INVALID_CONFIG", "Cannot read configuration JSON.") from exc
    allowed = {"repository", "stateDir", "database", "service", "commandTimeoutSeconds", "healthTimeoutSeconds", "productionEnvFile"}
    require(isinstance(data, dict) and not (set(data) - allowed), "INVALID_CONFIG", "Unknown configuration field.")
    for key in ["repository", "stateDir", "database"]:
        require(isinstance(data.get(key), str) and data[key], "INVALID_CONFIG", "Missing path: " + key)
        candidate = Path(data[key])
        data[key] = (path.parent / candidate).absolute() if not candidate.is_absolute() else candidate
        require(not data[key].is_symlink(), "UNSAFE_PATH", "Configured paths must not be symlinks.")
    repo, state, database = (data[k].resolve() for k in ["repository", "stateDir", "database"])
    require(not state.is_relative_to(repo) and not repo.is_relative_to(state), "UNSAFE_PATH", "State and source repository must be separate directories.")
    require(not database.is_relative_to(repo), "UNSAFE_PATH", "Live database must be outside source repository.")
    require(not database.is_relative_to(state / "releases") and not database.is_relative_to(state / "work"), "UNSAFE_PATH", "Database must not be inside release or candidate storage.")
    service = data.get("service")
    require(isinstance(service, dict) and set(service) == {"start", "stop", "healthUrl", "port"}, "INVALID_CONFIG", "service requires start, stop, healthUrl, and port.")
    command(service["start"]); command(service["stop"])
    require(type(service["port"]) is int and 1 <= service["port"] <= 65535, "INVALID_CONFIG", "Invalid service port.")
    parsed = urlparse(service["healthUrl"])
    require(parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"} and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
            "INVALID_CONFIG", "Health URL must be a local HTTP URL without credentials/query/fragment.")
    require(parsed.port == service["port"], "INVALID_CONFIG", "Health URL port must match service.port.")
    for key, default in [("commandTimeoutSeconds", 120), ("healthTimeoutSeconds", 15)]:
        data.setdefault(key, default)
        require(type(data[key]) in {int, float} and 0 < data[key] <= 3600, "INVALID_CONFIG", "Timeout must be between zero and 3600 seconds.")
    if data.get("productionEnvFile"):
        candidate = (path.parent / data["productionEnvFile"]).absolute()
        require(not candidate.is_symlink() and not candidate.resolve().is_relative_to(repo), "UNSAFE_PATH", "Production environment must be outside source repository.")
        data["productionEnvFile"] = candidate
    return data


def load_manifest(release: Path):
    try:
        value = json.loads((release / "release.json").read_text())
    except (OSError, ValueError) as exc:
        raise DeployError("INVALID_MANIFEST", "Release needs a valid release.json.") from exc
    require(isinstance(value, dict) and set(value) == {"version", "build", "test", "start", "compatibility", "migrations", "healthPath"}, "INVALID_MANIFEST", "Unexpected release manifest fields.")
    require(value["version"] == 1, "INVALID_MANIFEST", "Unsupported release manifest version.")
    require(isinstance(value["build"], list), "INVALID_MANIFEST", "build must be a list of commands.")
    for cmd in value["build"]:
        command(cmd)
    for key in ["test", "start", "compatibility"]:
        command(value[key])
    contained(release, value["migrations"])
    require(isinstance(value["healthPath"], str) and value["healthPath"].startswith("/") and not any(x in value["healthPath"] for x in ["?", "#", "\r", "\n"]), "INVALID_MANIFEST", "Invalid healthPath.")
    return value

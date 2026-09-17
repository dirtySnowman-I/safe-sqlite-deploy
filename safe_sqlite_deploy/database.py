from __future__ import annotations
import hashlib
from contextlib import contextmanager, closing
from pathlib import Path
import os
import re
import sqlite3
import time
from .util import require, DeployError

HISTORY = "_safe_deploy_migrations"
TOKEN = re.compile(r"\s+|--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|\d+(?:\.\d+)?|.")


@contextmanager
def connect(path: Path, read_only=False):
    require(path.exists() and path.is_file() and not path.is_symlink(), "INVALID_DATABASE", "Database must be an existing regular file, not a symlink.")
    db = sqlite3.connect(path.resolve().as_uri() + ("?mode=ro" if read_only else "?mode=rw"), uri=True, timeout=5)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    try:
        yield db
    finally:
        db.close()


def integrity(db):
    require(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "DATABASE_CORRUPT", "SQLite integrity check failed.")
    require(not db.execute("PRAGMA foreign_key_check").fetchall(), "FOREIGN_KEY_ERROR", "SQLite foreign key check failed.")


def backup_database(source: Path, destination: Path):
    require(not destination.exists(), "BACKUP_EXISTS", "Refusing to overwrite a backup.")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = destination.with_name(destination.name + ".tmp")
    require(not temp.exists(), "BACKUP_EXISTS", "Temporary backup already exists.")
    deadline = time.monotonic() + 30
    def progress(status, remaining, total):
        require(time.monotonic() < deadline, "BACKUP_TIMEOUT", "SQLite backup exceeded 30 seconds.")
    try:
        with connect(source, read_only=True) as src, closing(sqlite3.connect(temp)) as dst:
            src.backup(dst, pages=128, progress=progress, sleep=0.02)
            integrity(dst)
        os.chmod(temp, 0o600)
        with open(temp, "rb") as file:
            os.fsync(file.fileno())
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    with open(destination, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def statements(text):
    buffer = ""
    for char in text:
        buffer += char
        if char == ";" and sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""
    tokens = sql_tokens(buffer)
    if tokens:
        require(sqlite3.complete_statement(buffer + ";"), "INVALID_MIGRATION", "Incomplete SQL statement.")
        yield buffer


def sql_tokens(sql):
    result = []
    for match in TOKEN.finditer(sql):
        token = match.group()
        if token.isspace() or token.startswith("--") or token.startswith("/*"):
            continue
        if token.startswith("'"):
            result.append("<STRING>")
        elif token[0] in '\"`[':
            result.append("<IDENTIFIER>")
        else:
            result.append(token.upper())
    return result


def check_statement(sql):
    tokens = sql_tokens(sql)
    require(tokens, "INVALID_MIGRATION", "Empty migration statement.")
    allowed = tokens[:2] in [["CREATE", "TABLE"], ["CREATE", "INDEX"]]
    if tokens[:2] == ["ALTER", "TABLE"]:
        allowed = len(tokens) > 4 and tokens[3] == "ADD"
        forbidden = {"PRIMARY", "UNIQUE", "CHECK", "REFERENCES", "GENERATED"}
        require(not (set(tokens[4:]) & forbidden), "UNSAFE_MIGRATION", "ADD COLUMN constraints are not backward compatible.")
        if "NOT" in tokens and "NULL" in tokens:
            require("DEFAULT" in tokens, "UNSAFE_MIGRATION", "NOT NULL additions require a non-null constant default.")
    require(allowed, "UNSAFE_MIGRATION", "Only CREATE TABLE, nonunique CREATE INDEX, and compatible ADD COLUMN are accepted.")


def authorizer(action, arg1, arg2, database, trigger):
    # SQLite's parser is the second gate. Do not rely on keyword matching alone.
    if trigger or database not in {None, "main", "temp"}:
        return sqlite3.SQLITE_DENY
    if arg1 and arg1.lower().startswith("_safe_deploy"):
        return sqlite3.SQLITE_DENY
    if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
        return sqlite3.SQLITE_OK if arg1 in {"sqlite_master", "sqlite_schema"} else sqlite3.SQLITE_DENY
    allowed = {sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_READ,
               sqlite3.SQLITE_SELECT, sqlite3.SQLITE_ALTER_TABLE}
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK if arg2 in {"printf", "substr", "length", "sqlite_rename_table", "sqlite_rename_column", "sqlite_rename_test", "sqlite_drop_column"} else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def migration_files(root: Path):
    require(root.is_dir() and not root.is_symlink(), "MIGRATIONS_MISSING", "Migration directory is missing.")
    files = []
    for path in sorted(root.iterdir()):
        require(path.is_file() and not path.is_symlink() and re.fullmatch(r"\d{3,}_[a-z0-9_]+\.sql", path.name), "INVALID_MIGRATION", "Migration names must be numbered lowercase .sql files.")
        text = path.read_text()
        require(len(text.encode()) <= 1024 * 1024, "MIGRATION_LIMIT", "Migration exceeds 1 MiB.")
        pieces = list(statements(text))
        require(pieces, "INVALID_MIGRATION", "Empty migration file.")
        for piece in pieces:
            check_statement(piece)
        files.append((path.name, hashlib.sha256(text.encode()).hexdigest(), pieces))
    return files


def history(db):
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (HISTORY,)).fetchone()
    return dict(db.execute("SELECT name, sha256 FROM " + HISTORY)) if exists else {}


def plan_migrations(database: Path, root: Path):
    files = migration_files(root)
    with connect(database, read_only=True) as db:
        applied = history(db)
    incoming = {name: digest for name, digest, _ in files}
    require(all(incoming.get(name) == digest for name, digest in applied.items()), "MIGRATION_DRIFT", "Applied migrations are missing or have changed.")
    pending = [item for item in files if item[0] not in applied]
    require(not applied or all(name > max(applied) for name, _, _ in pending), "MIGRATION_ORDER", "New migrations must follow the applied sequence.")
    return pending


def migrate(database: Path, root: Path):
    pending = plan_migrations(database, root)
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("CREATE TABLE IF NOT EXISTS " + HISTORY + " (name TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
            for name, digest, pieces in pending:
                before = {row[0]: db.execute('SELECT sql FROM sqlite_master WHERE name=?', (row[0],)).fetchone()[0]
                          for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for sql in pieces:
                    db.set_authorizer(authorizer)
                    try:
                        db.execute(sql)
                    except sqlite3.DatabaseError as exc:
                        raise DeployError("MIGRATION_REJECTED", "SQLite rejected a migration or its permitted operations.") from exc
                    finally:
                        db.set_authorizer(None)
                # Verify newly added NOT NULL columns cannot reject old inserts for a null default.
                for table in before:
                    escaped = table.replace('"', '""')
                    for _, _, _, notnull, default, _ in db.execute('PRAGMA table_info("' + escaped + '")'):
                        if notnull and default is not None:
                            require(default.strip(' ()').upper() != "NULL", "UNSAFE_MIGRATION", "A NOT NULL column cannot default to NULL.")
                db.execute("INSERT INTO " + HISTORY + " VALUES (?,?)", (name, digest))
            integrity(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return [name for name, _, _ in pending]

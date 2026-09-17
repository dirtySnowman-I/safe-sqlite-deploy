import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from safe_sqlite_deploy.database import backup_database, migrate, plan_migrations, check_statement
from safe_sqlite_deploy.util import DeployError

class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "live.sqlite"
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, text TEXT NOT NULL)")
            db.execute("INSERT INTO notes(text) VALUES('keep')")
        self.migrations = self.root / "migrations"
        self.migrations.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def migration(self, text, name="001_change.sql"):
        (self.migrations / name).write_text(text)

    def test_additive_migration_is_once_only_and_checksum_protected(self):
        self.migration("ALTER TABLE notes ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;")
        self.assertEqual(migrate(self.db, self.migrations), ["001_change.sql"])
        self.assertEqual(migrate(self.db, self.migrations), [])
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT text,archived FROM notes").fetchone(), ("keep",0))
        self.migration("ALTER TABLE notes ADD COLUMN different INTEGER;")
        with self.assertRaises(DeployError) as error:
            plan_migrations(self.db, self.migrations)
        self.assertEqual(error.exception.code, "MIGRATION_DRIFT")

    def test_destructive_statements_and_unsupported_constraints_fail(self):
        for sql in ["DROP TABLE notes;", "DELETE FROM notes;", "UPDATE notes SET text='lost';", "ALTER TABLE notes RENAME TO gone;", "CREATE UNIQUE INDEX unique_text ON notes(text);", "PRAGMA writable_schema=1;", "ATTACH 'elsewhere' AS x;", "ALTER TABLE notes ADD COLUMN x TEXT NOT NULL;", "ALTER TABLE notes ADD COLUMN x TEXT CHECK(length(x)>0);", "CREATE TRIGGER evil AFTER INSERT ON notes BEGIN DELETE FROM notes; END;"]:
            with self.subTest(sql=sql), self.assertRaises(DeployError):
                check_statement(sql)

    def test_comments_and_strings_cannot_bypass_policy(self):
        with self.assertRaises(DeployError):
            check_statement("/* harmless */ DROP /* comment */ TABLE notes;")
        self.migration("ALTER TABLE notes ADD COLUMN info TEXT DEFAULT 'DROP TABLE notes;'; -- harmless literal\n")
        migrate(self.db, self.migrations)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT info FROM notes").fetchone()[0], "DROP TABLE notes;")

    def test_multi_statement_failure_rolls_back_all_ddl(self):
        self.migration("CREATE TABLE new_table(id INTEGER); CREATE TABLE notes(id INTEGER);")
        with self.assertRaises(DeployError):
            migrate(self.db, self.migrations)
        with sqlite3.connect(self.db) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='new_table'").fetchone())
            self.assertEqual(db.execute("SELECT text FROM notes").fetchone()[0], "keep")

    def test_authorizer_blocks_reserved_history_modification(self):
        self.migration("CREATE TABLE _safe_deploy_fake(id INTEGER);")
        with self.assertRaises(DeployError) as error:
            migrate(self.db, self.migrations)
        self.assertEqual(error.exception.code,"MIGRATION_REJECTED")

    def test_backup_includes_committed_wal_and_is_independent(self):
        writer=sqlite3.connect(self.db)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO notes(text) VALUES('in-wal')");writer.commit()
            destination=self.root/'backup.sqlite'
            digest=backup_database(self.db,destination)
            self.assertEqual(len(digest),64)
            writer.execute("INSERT INTO notes(text) VALUES('after')");writer.commit()
            with sqlite3.connect(destination) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM notes").fetchone()[0],2)
            self.assertEqual(destination.stat().st_mode & 0o777,0o600)
            with self.assertRaises(DeployError):backup_database(self.db,destination)
        finally:writer.close()

    def test_pending_migrations_must_follow_existing_sequence(self):
        self.migration("CREATE TABLE later(id INTEGER);", "002_later.sql")
        migrate(self.db,self.migrations)
        self.migration("CREATE TABLE earlier(id INTEGER);", "001_earlier.sql")
        with self.assertRaises(DeployError) as error:plan_migrations(self.db,self.migrations)
        self.assertEqual(error.exception.code,"MIGRATION_ORDER")

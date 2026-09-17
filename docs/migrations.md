# Migration policy

Migrations are ordered files such as `001_initial.sql`, `002_add_archive.sql`. Use a consistent zero-padded width: ordering is lexical. The directory must contain only numbered lowercase SQL files, at most 1 MiB each. Applied files remain present and unchanged forever. Their names and SHA-256 checksums are recorded in `_safe_deploy_migrations`.

## Accepted operations

- `CREATE TABLE` for new tables.
- Nonunique `CREATE INDEX`.
- `ALTER TABLE name ADD COLUMN` with a nullable column or a non-null constant default.

```sql
ALTER TABLE notes ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
CREATE INDEX notes_archived ON notes(archived);
```

ADD COLUMN cannot introduce `PRIMARY KEY`, `UNIQUE`, `CHECK`, `REFERENCES`, or generated-column constraints. A NOT NULL addition needs a non-null default. SQLite's own rules still apply; acceptance by the first gate does not guarantee the database will execute the statement.

## Rejected operations

DROP, rename, arbitrary INSERT/UPDATE/DELETE, PRAGMA, ATTACH, triggers, views, unique indexes, and schema rebuilds are outside V1. There is no force flag. Use a separately reviewed maintenance procedure for destructive changes or data backfills.

The first gate tokenizes SQL while ignoring comments and quoted literals, and splits statements with SQLite's completeness checker. A SQLite authorizer then limits actual operations during execution and protects migration-history objects. Integrity and foreign-key checks run before commit. The entire pending batch and its history commit in one transaction; a failed statement rolls back the batch.

## Why an additive gate is insufficient

Adding a nullable column can still break `INSERT INTO notes VALUES (...)` in old code. New runtime writes can also violate old application assumptions without violating SQLite constraints. Therefore the old runtime must perform a read/write compatibility test against a migrated copy before activation. Extend that test as the app grows. No static migration gate can establish arbitrary semantic compatibility.

The live service continues operating during candidate preparation. Activation stops all writers, takes a fresh backup, verifies that the migration history still matches the checked plan, and applies the same migrations to live data. Data changes between preparation and activation may still cause a migration to fail; transactional failure then triggers runtime rollback.

Rollback does not execute reverse migrations. Successful additive changes remain in the live database when the previous runtime returns. Its next release must retain every already-applied migration unchanged, including migrations introduced by a runtime release that later failed its health check.

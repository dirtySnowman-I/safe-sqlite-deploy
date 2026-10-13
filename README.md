# safe-sqlite-deploy

A single-host deployment CLI that tests a candidate release against a consistent copy of a SQLite database before touching the running service. It verifies the previous runtime can read and write the new schema, takes a fresh backup during activation, and rolls back the runtime if the new service fails health checks.

**Runtime rollback never restores an old database automatically.** Live writes remain intact. This is why migration restrictions and the previous release's compatibility test are both required.

## Try the complete demo

Requires **Linux, Python 3.11+, and Git**. The CLI and demo use the Python standard library; no accounts, external services, containers, or runtime packages are required.

```bash
git clone https://github.com/dirtySnowman-I/safe-sqlite-deploy.git
cd safe-sqlite-deploy
python3 -m unittest discover -s tests -v
python3 -m examples.demo
```

The demo creates its own Git repository, SQLite database, and local HTTP service. It proves four cases:

1. A first release becomes healthy.
2. An additive schema upgrade preserves existing notes.
3. A destructive migration is rejected before activation.
4. A release that fails only in production automatically returns to the previous runtime, preserving notes.

It stops its service afterward and leaves inspectable backups, logs, releases, and reports under `.demo/<run-id>/`. It never deploys this repository or touches an existing application.

## Use it with an application

Put a [`release.json`](examples/app/release.json) manifest and numbered SQL migrations in your application's Git repository. Keep operator configuration and the live database outside that repository. Adapt [config.example.json](config.example.json) and implement the service adapter contract in [docs/configuration.md](docs/configuration.md).

```bash
python3 -m safe_sqlite_deploy --config /srv/my-app/deploy.json check
python3 -m safe_sqlite_deploy --config /srv/my-app/deploy.json deploy --ref main
python3 -m safe_sqlite_deploy --config /srv/my-app/deploy.json recover
```

`check` is a read-only source/path preflight. The complete build, test, migration, and compatibility checks run during `deploy`. `recover` is for an interrupted deployment with an existing journal. Local refs are used as supplied; the tool never fetches, resets, or edits the source checkout. Deploying the already active revision returns `unchanged`; it is not a service restart or health-monitor command.

For a console command, optionally install in a virtual environment with `python3 -m pip install .`, then use `safe-sqlite-deploy` with the same arguments. Installation needs setuptools; running the module from the checkout does not.

## What it implements

- Clean Git source checks, immutable commit selection, and private release directories.
- Argument-array commands without an implicit shell; deadlines and private command logs.
- A minimal build environment without production environment values.
- SQLite online backups, integrity/foreign-key checks, and recorded SHA-256 digests.
- Append-only migration history with checksums, a SQL operation gate, and a SQLite authorizer.
- Real HTTP candidate checks with an exact revision and a fresh process-instance nonce.
- Mandatory read/write compatibility tests for both candidate and previous runtime.
- A single-host deployment lock, atomic release-pointer replacement, recovery journal, and reports.
- Automatic runtime rollback after activation failure, with the database left in place.

## Design and operation

Read [configuration and application contracts](docs/configuration.md), [the migration policy](docs/migrations.md), and [activation, rollback, and recovery](docs/operations.md).

| Module | Responsibility |
| --- | --- |
| `deployer.py` | Candidate validation, activation, rollback, and recovery |
| `database.py` | SQLite backups, migration policy, transactions, and history |
| `source.py` | Git preflight and source export |
| `health.py` | Exact revision/instance health checks |
| `config.py` | Operator and release-manifest validation |
| `util.py` | Locking, atomic journal writes, process control, and command execution |
| `examples/` | A real tiny application and local service adapter |
| `tests/` | Database, deployment, HTTP health, and failure-path tests |

## Boundaries

V1 targets one application, one existing SQLite database, one deployment process, and one Linux host. Activation includes downtime. It does not implement remote orchestration, zero-downtime deploys, destructive schema migrations, automatic data restoration, dependency sandboxing, backup retention, or a production process supervisor.

Application code and operator commands must be trusted. Environment isolation and separate directories are not an operating-system security sandbox: commands run as the deploying user. Compatibility tests provide evidence for the operations they exercise, not a proof that every application behavior is compatible. The supplied service adapter is a local demonstration; production must use supervised, idempotent start/stop hooks.

No software license is granted. This repository is public for portfolio inspection.

# Activation, rollback, and recovery

## Deployment sequence

1. Acquire a nonblocking host-local file lock. Refuse an unresolved journal.
2. Resolve a clean local Git commit. Reject source symlinks, submodules, and tracked environment-secret filenames. Export to a private release directory.
3. Copy SQLite using its online backup API, including committed WAL content. Migrate the isolated copy, then build and test the release.
4. Start the candidate on a local ephemeral port. Verify its exact revision and startup nonce, then run its read/write contract.
5. Start the previous runtime against another migrated copy and run its own read/write contract.
6. Recheck the source, journal activation intent, and stop production writers.
7. Take a fresh verified backup and record its checksum. Apply pending migrations transactionally.
8. Replace `current` with an atomic symlink, start the new runtime, and verify exact health.
9. Record the active release and a completed report; clear the journal.

Candidate checks run while the old service is available. The stop, fresh backup, migration, and startup interval is downtime. There is no distributed coordination; the operator must prevent unrelated tools from migrating the same database.

## Failure behavior

| Failure | Behavior |
| --- | --- |
| Dirty source, rejected migration, failed build/test/candidate | Reject before stopping the service |
| Live migration or new production health fails | Stop candidate, switch to previous release, start it, verify health |
| First release fails after activation | Stop candidate and remove active pointer; no previous runtime exists |
| Rollback fails | Preserve `recovery_required` journal and return an error |
| Interrupted process leaves journal | Refuse a new deploy until explicit recovery |

The CLI returns nonzero even when its automatic rollback succeeds: the requested deployment failed. JSON output/error codes go to stdout/stderr; command output remains in private logs. Cancellation by SIGINT/SIGTERM takes the normal cleanup/rollback path when possible. Do not repeatedly signal a process while it is rolling back.

## Recovery

```bash
python3 -m safe_sqlite_deploy --config /srv/my-app/deploy.json recover
```

Recovery acquires the same lock. A preactivation journal is archived without changing the active service. An activation journal causes conservative runtime rollback to the recorded previous release, followed by exact health verification. Failed recovery retains the journal. Do not delete a journal merely to bypass this guard.

The test suite reconstructs an interrupted activation journal and verifies recovery through a new Deployer instance. It does not claim exhaustive power-failure testing. Journal writes are atomic and fsynced, but filesystem, supervisor, and machine failures are separate fault domains.

SIGKILL or machine failure cannot run normal child cleanup. Before recovery, inspect the deployment's process group/service supervisor for leftover build or candidate processes and stop them. The tool does not persist every temporary process identity or manage a cgroup. Candidate work directories left by a hard kill can be removed once their processes are stopped. Use a supervisor to contain deployment and runtime processes in production.

## Backups and data safety

`backups/` contains preactivation SQLite snapshots. The backup API, rather than a raw file copy, includes committed WAL content. Each backup is integrity/foreign-key checked; reports record its SHA-256. Protect backups like the live database. Monitor disk space and implement retention/off-host copies appropriate for your application; V1 never silently deletes backups or old releases.

Automatic rollback changes the runtime only. It never replaces the live database with a snapshot: doing so could erase writes made after that snapshot. If manual data restoration is necessary, stop every writer, preserve the failed database and its WAL/SHM files for investigation, verify the chosen backup, explicitly assess which writes would be lost, and use a reviewed SQLite restoration procedure. `recover` is not a database-restore command.

## State and operating limits

The private state directory contains `releases/`, temporary `work/`, `backups/`, `logs/`, `reports/`, `current`, and `active.json`. The journal is present only during a deployment or unresolved recovery. Never edit an exported release or its revision marker after validation. Candidate and service logs may contain application output; limit access and arrange rotation. Build/test/hook command logs have a 20 MiB guard; long-running service logs require operator rotation.

The exported source archive is limited to 100 MiB, but application build artifacts have no enforced disk quota. SQLite integrity scans and migrations do not have a hard wall-clock kill boundary. Local ephemeral-port selection can race with another process; that attempt fails validation safely and can be retried. Compatibility checks use database copies and a clean environment, not a sandbox or container. Run the tool only on trusted application revisions and a local filesystem with working SQLite and flock semantics.

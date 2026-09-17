# Configuration and application contracts

## Operator configuration

Start with `config.example.json`. Relative paths resolve from the configuration file's directory. Source and state directories must be separate; the live database must be outside source, releases, and candidate work directories. The database must already exist. For a new application, create an empty SQLite database first; initial schema belongs in migration `001_*.sql`.

`productionEnvFile` is optional. When used, it is a JSON object of string environment values, outside the source repository, with mode 600 or stricter. These values go only to production service hooks. Never commit it. Deployment-owned keys such as `SQLITE_PATH`, `PORT`, and `DEPLOYMENT_INSTANCE` cannot be overridden. Keep state and database directories accessible only to the deployment/service account.

`commandTimeoutSeconds` bounds each build, test, compatibility, and service-hook command. `healthTimeoutSeconds` bounds each health wait. Defaults are 120 and 15 seconds; both must be positive and at most 3600. SQLite backups have a separate 30-second deadline. Backups and integrity checks can be expensive; this tool is intended for modest single-host databases.

## Release manifest

The committed application root must contain these exact fields:

```json
{
  "version": 1,
  "build": [["python3", "build.py"]],
  "test": ["python3", "test_app.py"],
  "start": ["python3", "server.py"],
  "compatibility": ["python3", "compatibility.py"],
  "migrations": "migrations",
  "healthPath": "/health"
}
```

`build` may be empty. Every command is an argument array, run in the exported release directory. Shell expansion, pipes, and redirects are not implicit. Commands must finish or stay in the foreground as appropriate: `start` is a foreground server, while build/test/compatibility commands exit zero on success. Builds may generate artifacts but must not change the manifest, revision marker, or migrations. Pin application dependencies and install them into the release, without relying on the source checkout or an inherited virtual environment.

The candidate database has already been migrated before build and tests. Build/test code receives no production secret environment, but runs as the same OS account: use trusted code only. Do not point application code at a hardcoded production database or external production API.

## Runtime environment

| Variable | Meaning |
| --- | --- |
| `SQLITE_PATH` | Isolated database during checks; live database for production hooks |
| `PORT` | Allocated local port for a candidate; configured port for production |
| `RELEASE_DIR` | Exported release directory |
| `RELEASE_REVISION` | Full selected Git commit |
| `DEPLOYMENT_INSTANCE` | Fresh nonce for this startup |
| `DEPLOY_MODE` | `candidate` or `production` |
| `DEPLOY_STATE` | Private deployment state directory |
| `BASE_URL` | Candidate URL, available to its compatibility command |

The environment also contains `PATH`, a private `HOME`, `LANG`, and Python buffering/bytecode settings. Other inherited application variables are discarded. Resolve the runtime's actual revision from the generated `DEPLOYED_REVISION` file. A health endpoint must query SQLite and return HTTP 200 with:

```json
{"status":"ok","database":"ok","revision":"<actual full commit>","instance":"<startup nonce>"}
```

Returning 200 alone is insufficient. The instance check prevents a stale process from passing a new startup check. Redirects are rejected, response bodies are bounded, and checks bypass HTTP proxy environment settings. The production health URL must be local HTTP and use the configured service port.

## Read/write compatibility

`compatibility` must exercise representative reads and writes using `BASE_URL` and/or `SQLITE_PATH`, assert the result, remove its probe rows, and exit zero. It only runs on isolated database copies. During upgrades, the previous release's own start command and compatibility command must pass against the newly migrated schema. Include constraints, insert column lists, defaults, and response shapes your old runtime depends on. A trivial test gives trivial assurance.

## Service adapter

Operator `service.start` and `service.stop` commands receive the production environment above. Argument placeholders are `{state}`, `{release}`, `{database}`, `{revision}`, `{instance}`, and `{port}`. Commands are not executed by a shell.

- Stop must be idempotent and wait until every app writer, worker, and relevant scheduled job is quiescent. It must succeed when nothing is running, including a first deployment.
- Start must launch the specified release with the supplied environment, arrange supervision, and return. It must not accidentally start a checkout or retain old environment values.
- Both hooks need to work during recovery and runtime rollback. A failed or partial start must be stoppable.

`examples/service.py` demonstrates the contract with a local Linux process group and PID identity checks. For production, implement hooks around your process supervisor. Calling `systemctl start` alone does not pass this command's environment to a unit: the adapter must explicitly configure the unit's release and private runtime environment. The deploying account needs narrowly scoped permissions to manage that service.

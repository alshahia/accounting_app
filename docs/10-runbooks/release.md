# Runbook — release and roll back the Al-Mahmoud artifact

- **Last verified:** 2026-10-26T15:45:00+03:00 by Build agent (production compose build + start executed on this host; see .local/prod-up.log and deploy/docker-compose.prod.yml)
- **Estimated duration:** 10–20 minutes (build + migrations + smoke)
- **Risk:** high — a release replaces the running product and runs schema migrations
- **Reversible:** yes while the previous image tag exists — roll back the image, then downgrade only if the migration is reversible (see step 6)

## Purpose

Take a committed revision, build the production image, migrate the database,
replace the running stack, and prove the product answers before declaring the
release done.

## Use when

- Promoting a release candidate to the pilot or production host.
- Rolling back a failed release within the same maintenance window.
- **Do not** release from a dirty working tree: the image must name committed bytes.

## Preconditions

- [ ] The revision under release is committed and tagged (git log --oneline -1; git tag --points-at HEAD).
- [ ] Gate run is green (scripts/ci-local.ps1 or the CI workflow run) on that revision.
- [ ] The target host has Docker 28+ with Compose v2.38+, the operator backup key, and a fresh encrypted backup from the last 24 hours.
- [ ] .env or the shell exports POSTGRES_PASSWORD, MAHMOUD_JWT_SECRET (at least 32 bytes), MAHMOUD_BOOTSTRAP_TOKEN (at least 32 bytes).
- [ ] TLS certificate and key are in deploy/certs (deploy/generate-certs.ps1 for a self-signed pilot certificate).

## Steps

### 0. New names in 0.2.0

The product was renamed (decision 0009). A fresh host simply starts from the new
names. A host that still runs the working-name installation keeps its data, but
the database name, the login role and the deployment secrets are operator
configuration, not schema, so the operator renames them once while the stack is
stopped:

~~~pwsh
$psql = 'C:\Program Files\PostgreSQL\18\bin\psql.exe'
& $psql -U postgres -c 'ALTER DATABASE ameen RENAME TO almahmoud'
& $psql -U postgres -c 'ALTER ROLE ameen RENAME TO almahmoud'
~~~

Then rewrite the stack env file with the new keys (`POSTGRES_DB=almahmoud`,
`POSTGRES_USER=almahmoud`, `MAHMOUD_JWT_SECRET`, `MAHMOUD_BOOTSTRAP_TOKEN`,
`MAHMOUD_IMAGE_TAG`) and re-register the scheduled backup tasks against the new
compose project:

~~~pwsh
pwsh -File deploy/backup/register-scheduled-backup.ps1 `
  -ComposeFile deploy/docker-compose.prod.yml `
  -ComposeEnvFile .local/pilot.env `
  -ComposeProject almahmoud-erp-prod
~~~

Migration `0023_rename_application_role` renames the internal application role
(`ameen_app` → `almahmoud_app`) automatically and keeps every grant; nothing
else inside the database has to be touched.

### 1. Record the exact artifact

~~~pwsh
git status --short
git log --oneline -1
$env:MAHMOUD_IMAGE_TAG = (git rev-parse --short HEAD)
docker build -f deploy/Dockerfile -t "almahmoud-erp-api:$env:MAHMOUD_IMAGE_TAG" .
~~~

### 2. Back up before the migration

~~~pwsh
pwsh -File deploy/backup/backup.ps1
pwsh -File deploy/backup/restore-drill.ps1
~~~

### 3. Validate the manifest with the release tag

~~~pwsh
$env:POSTGRES_PASSWORD = '<password>'
$env:MAHMOUD_JWT_SECRET = '<secret>'
$env:MAHMOUD_BOOTSTRAP_TOKEN = '<token>'
docker compose -f deploy/docker-compose.prod.yml config --quiet
~~~

### 4. Replace the stack

~~~pwsh
docker compose -f deploy/docker-compose.prod.yml up -d --build
docker compose -f deploy/docker-compose.prod.yml ps
~~~

The migrate service must exit 0 before api starts; api and gateway become healthy.

### 5. Smoke the release

~~~pwsh
curl.exe -sSI --insecure https://127.0.0.1/ | Select-String 'HTTP/|strict-transport-security|x-content-type-options|content-security-policy'
curl.exe -sS --insecure https://127.0.0.1/api/v1/health
# expect HTTP/1.1 200 and {"status":"ok",...}
~~~

Then sign in through the browser at https://<host>/ and open one report and the
administration screen. A release is not done until a human has signed in.

### 6. Roll back

~~~pwsh
# image rollback (fastest): previous tag must still exist on the host
$env:MAHMOUD_IMAGE_TAG = '<previous-short-sha>'
docker compose -f deploy/docker-compose.prod.yml up -d --no-build
~~~

If the new release migrated the schema and the change is not backward
compatible, the image rollback is not enough: restore the encrypted dump from
step 2 into a fresh database (backup-restore.md step 5), then start the previous
image against it. Never downgrade a production schema with alembic downgrade
unless that exact path was rehearsed on a copy.

## Verification

~~~pwsh
docker compose -f deploy/docker-compose.prod.yml ps
curl.exe -sS --insecure https://127.0.0.1/api/v1/health
# expect: db, migrate (exited 0), api and gateway present; health returns status ok
~~~

## Rollback

1. Image rollback as in step 6.
2. If migrations ran and the previous image cannot read the schema, restore the pre-release backup (backup-restore.md) and only then start the old image.
3. Record the rollback in docs/03-handoff.md with the failed image tag and the reason.

## Failure modes

| Symptom | Cause | Action |
|---|---|---|
| migrate exits non-zero | schema conflict or database unreachable | read docker compose logs migrate; fix the migration on a copy, never edit a released migration |
| api unhealthy, migrate was green | missing secret or bootstrap token with production checks | docker compose logs api; settings refuse short secrets by design |
| gateway 502 | api not healthy yet | wait for the api healthcheck, then retry; the gateway retries per request |
| certificate error in browser | self-signed pilot certificate | install deploy/certs/server.crt in the pilot workstation trust store or use the organisation CA |
| compose build tries to download | base images not present | pre-pull on a connected host and docker save/load; the running stack downloads nothing |

## Notes

- The build is two stages: pnpm compiles the frontend into the image, then the API serves web/dist from the same origin.
- Migrations run from /srv/.venv/bin/alembic in a one-shot service, not from the API start-up command.
- Keep the last two image tags on disk so a rollback costs seconds, not a rebuild.
- The backup file marker `AMEENBK1` is a persisted format identifier from the working-name era; it is deliberately unchanged, so backups taken before 0.2.0 still restore.

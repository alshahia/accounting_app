# Runbook — incident response and recovery

- **Last verified:** 2026-10-26T16:05:00+03:00 by Build agent (restore path exercised end-to-end in backup-restore.md during this window)
- **Estimated duration:** 15 minutes to contain; recovery depends on the restore step (5–20 minutes)
- **Risk:** high — recovery overwrites or rolls back data
- **Reversible:** no — a point-in-time recovery discards changes after the target; say so out loud before doing it

## Purpose

Contain a production incident, preserve evidence, restore service with a known
data state, and leave a written record that a later review can act on.

## Use when

- The API is down, data is wrong or lost, the database is unavailable, or an unauthorised action is suspected.
- **Do not** restart services repeatedly hoping for a different result: three restarts without a new fact is the signal to restore.

## Preconditions

- [ ] You can reach the host and run docker compose in the deployment directory.
- [ ] The operator backup key and the newest encrypted dump are available (deploy/backup).
- [ ] You know the last known-good time (the incident start, or the last release).

## Steps

### 1. Contain (first 5 minutes)

~~~pwsh
cd <deployment directory>
docker compose -f deploy/docker-compose.prod.yml ps
docker compose -f deploy/docker-compose.prod.yml logs --tail 200 api
docker compose -f deploy/docker-compose.prod.yml logs --tail 100 db
~~~

- If data is actively being corrupted, stop the API and gateway (do not stop the database): docker compose -f deploy/docker-compose.prod.yml stop api gateway
- Write down the UTC time of containment. That time becomes a candidate recovery target.

### 2. Preserve evidence

~~~pwsh
docker compose -f deploy/docker-compose.prod.yml logs > incident-api-$(Get-Date -Format yyyyMMddTHHmmssZ).log
docker compose -f deploy/docker-compose.prod.yml exec db sh -c 'ls -l $PGDATA/pg_wal_archive | wc -l'
pwsh -File deploy/backup/backup.ps1
~~~

Take a fresh encrypted dump before changing anything; it is the forensic copy
of the failed state.

### 3. Classify the failure

| Evidence | Class | Next |
|---|---|---|
| api restart loop, migration error in logs | release failure | release.md step 6 (image rollback) |
| db refuses connections, disk full, corrupted relation | database failure | restore the newest dump (backup-restore.md step 5) |
| wrong postings after a user action | data error | restore to a target time before the action; keep the failed-state dump for forensics |
| audit log or permissions tampering suspected | security incident | keep both dumps, notify the owner, do not recover before preserving evidence |

### 4. Recover

Follow backup-restore.md: decrypt the newest dump, restore into a fresh
database, run the drill comparison, and start the stack against it. For a
release failure, follow release.md step 6 first: image rollback is cheaper than
a restore.

### 5. Verify service

~~~pwsh
curl.exe -sS --insecure https://127.0.0.1/api/v1/health
pwsh -File deploy/backup/restore-drill.ps1
~~~

Then sign in as the accountant and check the trial balance for the affected
period against the last printed statement.

### 6. Write the record

Append to docs/03-handoff.md: start and containment times, class, action taken,
data loss window (if any), and the verification result. Link the incident log
and the report JSON. Keep the failed-state dump until the owner agrees to
delete it.

## Verification

- health endpoint returns 200 with status ok,
- the restore drill is green,
- the accountant confirms the trial balance for the affected period.

## Rollback

Recovery cannot be undone: changes after the recovery target are discarded.
The pre-recovery dump taken in step 2 is the only way back to the failed state,
and it exists so the incident can be investigated, not to resume normal work.

## Failure modes

| Symptom | Cause | Action |
|---|---|---|
| restore drill red | wrong dump or wrong database target | compare manifest database and migration revision; rerun with the matching dump |
| API healthy but all requests 500 | database role/permission mismatch after restore | check MAHMOUD_DATABASE_URL and the almahmoud_app grants; a dump restores roles, not connection strings |
| users cannot sign in after restore | sessions are in the restored database and tokens may reference old session ids | delete expired sessions or sign in again; expected |
| disk full during recovery | WAL + dump + scratch database | free space first; scratch restore needs roughly the database size again |

## Notes

- The blast radius of a restore is the whole database, not one company. Announce it.
- Audit rows are append-only at the database level; a recovery that rewinds the audit log is a claim about the past that must be documented.
- Keep this runbook short enough to read during an incident; detail lives in backup-restore.md and release.md.

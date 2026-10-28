# Runbook — back up and restore the Al-Mahmoud database

- **Last verified:** 2026-10-27T08:31:00+03:00 by Build agent (deployment-mode backup + drill on the pilot compose stack, report drill-20261027T053100Z.json; host-mode backup + drill on the dev cluster, report drill-20261027T052646Z.json)
- **Estimated duration:** 2–5 minutes logical backup + drill; 10–20 minutes point-in-time recovery
- **Risk:** medium — a restore overwrites a database; the drill always targets a scratch database
- **Reversible:** yes — the drill drops only the scratch database and temporary files it created

## Purpose

Take an encrypted, scheduled PostgreSQL backup of the Al-Mahmoud database, prove a
restore reproduces the recorded trial balance to the minor unit, and recover to
a point in time from the archived WAL.

## Use when

- Daily at 02:00 (the scheduled backup task) and 03:00 (the scheduled drill).
- Before a migration that rewrites data, or before a release rehearsal.
- After an incident that requires recovery to a point before the damage.
- **Do not** run the drill against the production database itself: it creates and
  drops only a scratch database named almahmoud_restore_drill on the same server.

## Two modes

| Mode | When | How the dump is taken |
|---|---|---|
| deployment | the pilot/production stack from deploy/docker-compose.prod.yml | pg_dump runs inside the compose db service, so the database keeps no host port (AC-16); the host only sees the encrypted file |
| host/server | a database reachable directly (dev cluster on 127.0.0.1:55432, a managed server) | pg_dump/psql on the host with -DatabaseUrl |

The manifest and the drill are the same in both modes. Deployment mode is the
one the scheduled tasks use: it covers the data the pilot actually writes.

## Preconditions

- [ ] PostgreSQL client binaries for host mode: C:\Program Files\PostgreSQL\18\bin. Deployment mode needs only Docker.
- [ ] Docker Desktop installed and running for deployment mode; the scheduled wrapper starts it and waits up to six minutes.
- [ ] The operator holds the backup key file (deploy/backup/keys/backup.key, at least 32 random bytes). **The key never enters the repository; losing it makes every encrypted dump unrecoverable.**
- [ ] Deployment mode: a compose env file with the stack secrets exists outside git, for example .local/pilot.env (POSTGRES_PASSWORD, MAHMOUD_JWT_SECRET, MAHMOUD_BOOTSTRAP_TOKEN). It is git-ignored.
- [ ] Host mode: MAHMOUD_BACKUP_DATABASE_URL in the operator shell, or pass -DatabaseUrl.
- [ ] Working directory is the repository root (E:\rea\ameen_app).
- [ ] Free disk space on deploy/backup/dumps (a pilot-sized encrypted dump is a few hundred kB; a production dump is roughly the database size).

## Steps

### 1. One-time setup: key, directories, schedule

~~~pwsh
$bytes = [byte[]]::new(64)
[System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
New-Item -ItemType Directory -Force deploy/backup/keys, deploy/backup/dumps, deploy/backup/reports
[System.IO.File]::WriteAllBytes('deploy/backup/keys/backup.key', $bytes)
# copy the key to the operator's password manager / offline media, then either:
pwsh -File deploy/backup/register-scheduled-backup.ps1 -ComposeFile deploy/docker-compose.prod.yml -ComposeEnvFile .local/pilot.env -ComposeProject almahmoud-erp-prod
# or, for a directly reachable database:
pwsh -File deploy/backup/register-scheduled-backup.ps1 -DatabaseUrl 'postgresql://almahmoud:<password>@127.0.0.1:55432/almahmoud'
~~~

The registration writes two daily tasks (Al-Mahmoud Backup 02:00, Al-Mahmoud Restore
Drill 03:00) that call deploy/backup/run-scheduled.ps1. The wrapper starts
Docker Desktop when the machine has just rebooted, waits for the engine, then
runs the backup or the drill and returns its exit code as the task result.
Task Scheduler therefore shows a failed run as a non-zero result instead of a
silent gap. On a Linux host the equivalent schedule is two cron entries under
the operator account, not root.

### 2. Run an encrypted backup now

~~~pwsh
# deployment mode (pilot stack)
pwsh -File deploy/backup/backup.ps1 -ComposeFile deploy/docker-compose.prod.yml -ComposeEnvFile .local/pilot.env -ComposeProject almahmoud-erp-prod

# host mode (dev cluster)
$env:MAHMOUD_BACKUP_DATABASE_URL = 'postgresql://almahmoud:<password>@127.0.0.1:55432/almahmoud'
pwsh -File deploy/backup/backup.ps1
~~~

Expected output: a line starting "backup ok:" pointing at
deploy/backup/dumps/almahmoud-<UTC stamp>.dump.enc, then the manifest path and its
consistency. The plaintext dump is deleted in the same run; the manifest records
both SHA-256 hashes plus the trial-balance digest, journal totals and migration
revision captured when the dump was taken.

### 3. Run the restore drill now

~~~pwsh
pwsh -File deploy/backup/restore-drill.ps1 -ComposeFile deploy/docker-compose.prod.yml -ComposeEnvFile .local/pilot.env -ComposeProject almahmoud-erp-prod
~~~

The script decrypts the newest dump, recreates almahmoud_restore_drill (inside the
database container in deployment mode), restores it, and compares the
per-account trial balance digest, the journal totals and the migration revision
with the values recorded in the backup manifest — not with today's live rows, so
business activity after the backup cannot turn a correct restore into a red
run. It drops the scratch database and the temporary files, writes a JSON report
to deploy/backup/reports/drill-<UTC stamp>.json, and appends one line to the
access log.

### 4. Copy the backups and WAL off-host

~~~pwsh
# after the 02:00 job; destination is operator-encrypted removable media / NAS
Copy-Item deploy/backup/dumps/*.dump.enc \\backup-host\almahmoud\ -Force
Copy-Item deploy/backup/keys/backup.key \\backup-host\almahmoud\-key\ -Force  # separate, access-controlled location
docker compose -f deploy/docker-compose.prod.yml exec db sh -c "cp -a /var/lib/postgresql/18/docker/pg_wal_archive/. /tmp/wal-export/"
# container WAL path: /var/lib/postgresql/18/docker/pg_wal_archive; copy it to the same encrypted destination
~~~

The dump without the key is not a backup, and the key without the dump is not a
backup either: keep them in two different places with different access.

### 5. Point-in-time recovery (container stack)

1. Stop the API: docker compose -f deploy/docker-compose.prod.yml stop api gateway
2. Keep the damaged cluster for forensics: docker compose -f deploy/docker-compose.prod.yml stop db
3. Restore the newest encrypted dump into a fresh database named almahmoud on a new volume (decrypt with deploy/backup/common.ps1 Unprotect-File, then pg_restore).
4. Recover the archived WAL to a target timestamp (recovery settings are merged into postgresql.conf since PostgreSQL 12):

~~~sh
cat >> "$PGDATA/postgresql.conf" <<'EOF'
restore_command = 'cp /var/lib/postgresql/18/docker/pg_wal_archive/%f %p'
recovery_target_time = '2026-10-27 08:00:00+03'
recovery_target_action = 'promote'
EOF
touch "$PGDATA/recovery.signal"
~~~

5. Start the database, confirm "database system is ready to accept connections", then run the drill comparison against the recovered database (-DatabaseUrl pointing at it).
6. Start api and gateway again; sign in and confirm the trial balance report matches the expected period.

If the logical dump alone is sufficient (no transactions need replay), skip the
WAL step: restore the dump, start the stack, verify with the drill. This is the
normal recovery path for a failed migration.

## Verification

~~~pwsh
pwsh -File deploy/backup/restore-drill.ps1 -ComposeFile deploy/docker-compose.prod.yml -ComposeEnvFile .local/pilot.env -ComposeProject almahmoud-erp-prod
# expect: final line "restore drill: green -> ...drill-<stamp>.json"
Get-Content deploy/backup/dumps/backup-access.log -Tail 2
# expect: one backup line and one restore-drill line with status ok / green
Get-ScheduledTaskInfo -TaskName 'Al-Mahmoud Backup','Al-Mahmoud Restore Drill' | Select-Object TaskName,LastTaskResult
# expect: LastTaskResult 0 for both
~~~

## Rollback

1. The drill drops almahmoud_restore_drill itself; if a run was interrupted, run: psql -d postgres -c "DROP DATABASE IF EXISTS almahmoud_restore_drill WITH (FORCE);" (or the same through docker compose exec db psql).
2. Delete any decrypted temporary file left in %TEMP% named almahmoud-drill-*.dump.
3. No step writes to the source database; rolling back a backup run means deleting that dump and its manifest (and logging the deletion in the access log).

## Failure modes

| Symptom | Cause | Action |
|---|---|---|
| Docker engine did not answer within 6 minutes | Docker Desktop not installed or failed to start after reboot | start Docker Desktop by hand, confirm docker info answers, then run the task again; the task result stays non-zero until it works |
| backup key file not found | key not created or wrong -KeyFile | restore the key from the operator password manager; never regenerate it while old dumps must remain readable |
| AES-GCM authentication failed | wrong key or truncated/corrupted file | try the previous dump; the manifest SHA-256 identifies truncation |
| pg_restore failed with exit code 1 | dump taken from a different schema revision | check the manifest migration revision; restore the matching release image |
| restore drill: red, "differs from the backup manifest" | the dump really does not reproduce the recorded state (corruption, partial restore, wrong key) | keep both reports, do not delete the dump; restore the previous dump and investigate before the next backup cycle |
| restore drill: red, "connection refused"/psql failed | the database or the Docker engine is down when the task fires | start the engine/database and re-run; a red report is the correct signal, never edit it |
| psql: DROP DATABASE ... is being accessed by other users | an API still connected to the scratch DB | re-run; the script uses WITH (FORCE) and drops after pg_restore |
| container WAL archive empty | archive_mode not enabled | confirm the db command includes archive_mode=on and archive_command; restart the db service |
| scheduled task result 1 after a reboot | Docker Desktop started too late for the 02:00 window | the wrapper waits six minutes; if the host was off, use -StartWhenAvailable behaviour by running the task manually and confirm the next night is green |

## Notes

- **Encryption at rest:** dumps are AES-256-GCM encrypted before they leave the process, with a PBKDF2-SHA256 (200,000 iterations) key derived from the operator key. The manifest records both hashes; the access log records every backup, retention deletion and drill.
- **Manifest instead of live comparison:** the drill compares the restored database with the digest, totals and migration revision captured at backup time. This is what makes a nightly drill meaningful on a live pilot: yesterday's dump is still correct after today's invoices.
- **WAL:** production compose enables archive_mode=on with archive_timeout=300 and archives into $PGDATA/pg_wal_archive (inside the pgdata volume). Combine with a weekly pg_basebackup for full PITR; the WAL segments alone are not a backup.
- **Retention:** the newest 14 dumps and their manifests are kept; older ones are deleted by the backup script and the deletion is logged.
- **Verified numbers (2026-10-27):** deployment-mode dump 284 kB, backup 2.7 s, drill 8 s, green at migration 0022; host-mode dump 515 kB, green at migration 0022. Both scratch databases were dropped afterwards.
- **Host dependency:** the scheduled tasks run in the operator's Windows session; the machine must be logged on (or the task configured to run whether or not the user is logged on, which needs the account password). Record the choice in docs/02-environment.md.
- **The legacy dev drill** (scripts/backup_restore_drill.py, unencrypted, build/backup/) is kept for the phase-1 acceptance evidence; the product path is deploy/backup/*.

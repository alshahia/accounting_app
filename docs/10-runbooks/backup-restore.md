# Runbook — back up and restore the Al-Mahmoud database

- **Last verified:** the backup and restore-drill path was last exercised 2026-11-16T08:21Z, with dump almahmoud-20261116T082137Z.dump.enc and report drill-20261116T082146Z.json; the gateway/health path was re-verified separately 2026-11-16T13:17:00+03:00 by T-38 (gateway restarted; plain HTTP on 8090 answers /api/v1/health and /api/v1/health/ready 200 directly instead of 301, HTTPS on 8443 stays 200, and api, db and gateway all report healthy); previously 2026-11-16T13:00:00+03:00 by T-44 (a red health check now exits 1 with or without a mailbox); previously 2026-11-16T12:54:00+03:00 by T-35/T-43 (pilot stack recovered on port 8090; scheduled backup and drill re-run green against the live stack — dump almahmoud-20261116T082137Z.dump.enc, report drill-20261116T082146Z.json, both task results 0 — and the health watch now registered as SYSTEM from an elevated shell, hidden, at startup and every 15 minutes, with its manual and first scheduled runs both result 0; 8090 answered 301, not 200, before T-38 fixed the nginx redirect). Previously 2026-10-27T08:31:00+03:00 by Build agent (deployment-mode backup + drill on the pilot compose stack, report drill-20261027T053100Z.json; host-mode backup + drill on the dev cluster, report drill-20261027T052646Z.json)
- **Estimated duration:** 2–5 minutes logical backup + drill; 10–20 minutes point-in-time recovery
- **Risk:** medium — a restore overwrites a database; the drill always targets a scratch database
- **Reversible:** yes — the drill drops only the scratch database and temporary files it created

## Purpose

Take an encrypted, scheduled PostgreSQL backup of the Al-Mahmoud database, prove a
restore reproduces the recorded trial balance to the minor unit, and recover to
a point in time from the archived WAL.

And watch the pilot between backups: one health probe every 15 minutes that
records its result locally. A red check exits 1 — with or without a mailbox —
so Task Scheduler records a failed result and the task history is the alarm
(T-44). A configured mailbox adds off-host delivery, one mail per 60 minutes;
it no longer decides the exit code (owner question Q4 covers provisioning it).

## Use when

- Daily at 02:00 (the scheduled backup task) and 03:00 (the scheduled drill).
- Every 15 minutes (the scheduled health watch) whenever you need to know whether
  the pilot is still answering.
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
- [ ] Optional, for alert mail: .local/alert.env carries the four mandatory ALERT_* keys (ALERT_MAIL_TO, ALERT_MAIL_FROM, ALERT_SMTP_HOST, ALERT_SMTP_PORT) and, only if the provider authenticates, the optional ALERT_SMTP_USER / ALERT_SMTP_PASSWORD pair — set both or neither, because a half-configured pair fails closed. ALERT_SMTP_PORT must be a number in 1-65535; anything else also fails closed with the reason logged. The file is git-ignored and never committed; without it a red health check is logged locally and still exits 1 — only the mail is missing (T-44).
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

### 4. Watch the pilot health every 15 minutes

~~~pwsh
# one-time registration; run it from an elevated shell: SYSTEM with an
# at-startup trigger is the default and the requirement
pwsh -File deploy/backup/register-health-watch.ps1 -RequireSystem
# only if this host cannot be elevated yet, and only deliberately:
pwsh -File deploy/backup/register-health-watch.ps1 -AllowInteractiveFallback
# run the watch once, exactly as the task does
pwsh -File deploy/backup/watch-health.ps1
Get-Content deploy/backup/reports/health-watch.log -Tail 5
~~~

`Al-Mahmoud Health Watch` runs `deploy/backup/watch-health.ps1` hidden, every 15
minutes, and records its exit code as the task result. From an elevated shell it
also runs at startup, as SYSTEM with no interactive logon. An unelevated run
refuses to register anything unless `-AllowInteractiveFallback` is passed, and
even then it throws rather than overwrite a task that belongs to another
principal, so an unelevated rerun cannot silently downgrade a SYSTEM task.
**On this host the live task is the SYSTEM task**, registered 2026-11-16T12:48
from an elevated shell: it runs as `SYSTEM` (`S-1-5-18`, runlevel Highest,
hidden) at startup and every 15 minutes with no interactive logon, and its first
manual (12:48:27) and scheduled (12:49:19) runs both returned task result 0 — so
a logoff or reboot no longer stops the watch (T-43). The interactive fallback
remains for hosts that cannot be elevated; the live principal is recorded in
`docs/02-environment.md`, so update both places when it changes.

The watch probes `http://127.0.0.1:8090/api/v1/health`. Since T-38 the pilot
gateway forwards the health and readiness paths to the API on plain HTTP, so the
default probe answers **200 directly** and no redirect has to be followed. The
watch keeps its redirect-following behaviour on `-HttpsPort 8443` — only while a
redirect stays on the configured pilot host, never for an external target — and
that path now matters only if a redirect is reintroduced. A 301 would prove only
that nginx answered, not that the API is healthy. Every *other* path on port 80
still answers 301 to TLS, and that redirect cannot name the host-published HTTPS
port, so it points at :443 and is correct only when the gateway publishes 443
(T-35, documented limitation). Each run appends one line to
`deploy/backup/reports/health-watch.log` (renamed to `health-watch.log.1` at
1 MB) and rewrites `deploy/backup/reports/health-watch-state.json` with the last
result, the consecutive-failure count, the last alert time and — when alerting
is unavailable — the exact reason.

Mail is config-gated. `.local/alert.env` must carry four mandatory keys —
`ALERT_MAIL_TO`, `ALERT_MAIL_FROM`, `ALERT_SMTP_HOST`, `ALERT_SMTP_PORT` — plus
`ALERT_SMTP_USER` / `ALERT_SMTP_PASSWORD` only when the provider authenticates;
that pair is optional but must be complete, so a user without a password or a
password without a user also fails closed, and the port must be a number in
1-65535. A red run exits 1 in every case — gate closed, throttled, sent or
send-failed (T-44) — so the task result is the alarm even while no mailbox
exists. When the gate is closed the run logs a WARNING naming the exact reason
and records `alert=not-configured`; once the gate is open a red run also sends
one message through `Send-MailMessage`, throttled to one per 60 minutes by the
state file. Provisioning that mailbox is owner question Q4: it adds off-host
delivery and is no longer what decides the exit code.

Mail transport: **587 with STARTTLS** (`-UseSsl`) is the supported and only
tested path. Port 465 is usually implicit TLS, which `Send-MailMessage` does not
speak, so 465 is **unsupported until someone tests it** against the chosen
provider.

### 5. Copy the backups and WAL off-host

~~~pwsh
# after the 02:00 job; destination is operator-encrypted removable media / NAS
Copy-Item deploy/backup/dumps/*.dump.enc \\backup-host\almahmoud\ -Force
Copy-Item deploy/backup/keys/backup.key \\backup-host\almahmoud\-key\ -Force  # separate, access-controlled location
docker compose -f deploy/docker-compose.prod.yml exec db sh -c "cp -a /var/lib/postgresql/18/docker/pg_wal_archive/. /tmp/wal-export/"
# container WAL path: /var/lib/postgresql/18/docker/pg_wal_archive; copy it to the same encrypted destination
~~~

The dump without the key is not a backup, and the key without the dump is not a
backup either: keep them in two different places with different access.

### 6. Point-in-time recovery (container stack)

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
foreach ($name in 'Al-Mahmoud Backup','Al-Mahmoud Restore Drill','Al-Mahmoud Health Watch') {
    Get-ScheduledTaskInfo -TaskName $name | Select-Object TaskName,LastRunTime,LastTaskResult
}
# expect: LastTaskResult 0 for all three
Get-Content deploy/backup/reports/health-watch.log -Tail 1
# expect: check=green ... http=200 (a red line carries alert=not-configured or alert=sent, and that run exits 1)
Get-ScheduledTask -TaskName 'Al-Mahmoud Health Watch' | Select-Object TaskName,State,@{n='User';e={$_.Principal.UserId}}
# expect: Ready; SYSTEM (the live principal on the pilot host; the operator account only on a -AllowInteractiveFallback host)
Invoke-WebRequest -UseBasicParsing https://127.0.0.1:8443/api/v1/health -SkipCertificateCheck
# expect: 200 and a body starting {"status":"ok"; plain HTTP on 8090 also answers 200 for /api/v1/health (T-38) and 301 only for other paths
~~~

## Rollback

1. The drill drops almahmoud_restore_drill itself; if a run was interrupted, run: psql -d postgres -c "DROP DATABASE IF EXISTS almahmoud_restore_drill WITH (FORCE);" (or the same through docker compose exec db psql).
2. Delete any decrypted temporary file left in %TEMP% named almahmoud-drill-*.dump.
3. No step writes to the source database; rolling back a backup run means deleting that dump and its manifest (and logging the deletion in the access log).

## Reload or roll back the gateway configuration

The gateway bind-mounts `deploy/nginx.conf`, so a config change needs a reload,
not a recreate — and a recreate must not take the database with it.

1. Validate inside the running container: `docker exec almahmoud-erp-prod-gateway-1 nginx -t`
2. Reload without restarting anything: `docker exec almahmoud-erp-prod-gateway-1 nginx -s reload`
3. Re-probe: `http://127.0.0.1:8090/api/v1/health` and `/api/v1/health/ready`
   answer 200, `https://127.0.0.1:8443/api/v1/health` answers 200, and every other
   port-80 path still answers 301.

If the container itself has to be recreated (image or entrypoint change), recreate
only the gateway:

~~~pwsh
docker compose --project-name almahmoud-erp-prod --env-file .local/pilot.env -f deploy/docker-compose.prod.yml up -d --no-deps gateway
~~~

**Take a fresh dump (step 2) before any `up` that can touch the database** — for
example the `--wait` recovery command in the failure-mode table, or a plain
`up -d gateway` if the config-hash drift returns. On 2026-11-16 the plain `up -d gateway` prescribed by
the T-38 brief recreated **db, migrate, api and gateway** — all four Created times
moved to 10:14:34-38Z — because of pre-existing config-hash drift on db, migrate
and api that was never identified. The data survived only because the
`almahmoud-erp-prod_pgdata` volume predates the project, migrate re-ran to
revision `0023` and exited 0, and a fresh 08:21Z dump existed. A gateway-only
change must never be able to restart the database.

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
| health watch red, "No connection could be made" | the stack is stopped, or the gateway no longer publishes 8090 | docker compose --project-name almahmoud-erp-prod --env-file .local/pilot.env -f deploy/docker-compose.prod.yml up -d --wait --wait-timeout 300, then pwsh -File deploy/backup/watch-health.ps1 |
| health watch red, "health body status=..." | the API answered 200 but is not ok | read the API log: docker compose --project-name almahmoud-erp-prod --env-file .local/pilot.env -f deploy/docker-compose.prod.yml logs --tail 100 api |
| health watch logs alert=not-configured | .local/alert.env is absent or incomplete: a mandatory key is missing, the user/password pair is half-configured, or ALERT_SMTP_PORT is not a number in 1-65535 — the line's reason= names the exact cause | add the four mandatory ALERT_* keys, and the optional user/password pair as a pair (owner Q4); the red check is in health-watch.log and the task result is already 1, only the mail is missing |
| register-health-watch.ps1 reports "Access is denied" or refuses | the shell is not elevated: SYSTEM with an at-startup trigger is the default and needs an administrator | re-run from an elevated shell with -RequireSystem; an unelevated run registers nothing unless -AllowInteractiveFallback is passed, and that fallback throws rather than overwrite a task that belongs to another principal |

## Notes

- **Encryption at rest:** dumps are AES-256-GCM encrypted before they leave the process, with a PBKDF2-SHA256 (200,000 iterations) key derived from the operator key. The manifest records both hashes; the access log records every backup, retention deletion and drill.
- **Manifest instead of live comparison:** the drill compares the restored database with the digest, totals and migration revision captured at backup time. This is what makes a nightly drill meaningful on a live pilot: yesterday's dump is still correct after today's invoices.
- **WAL:** production compose enables archive_mode=on with archive_timeout=300 and archives into $PGDATA/pg_wal_archive (inside the pgdata volume). Combine with a weekly pg_basebackup for full PITR; the WAL segments alone are not a backup.
- **What the edge can see (T-38):** every host/LAN client reaches nginx through Docker's published-port proxy, so the peer nginx forwards — and the API records — is that proxy's address, one address shared by all LAN clients, not the end client's IP. The API therefore keeps a single global login-limiter bucket (20 failed sign-in attempts per minute, fixed window) for all external clients: one attacker can lock every user out for the rest of the window, and session/audit rows carry the same address. That is the accepted pilot identity model; the guarantee T-38 adds is only that a client cannot choose the address (nginx overwrites the forwarded chain with `$remote_addr`).
- **Gateway access log vs the app record:** the gateway's default `log_format main` ends with `$http_x_forwarded_for`, which is the **client-supplied** header, while the app records the peer nginx overwrote it with. The two deliberately disagree; do not read the access log's last field as the client address (T-38 review, n2).
- **Retention:** the newest 30 dumps and their manifests are kept (raised from 14 on 2026-11-16, T-45); older ones are deleted by the backup script and the deletion is logged. The scheduled task passes no override, so the script default `RetainCount` is the live policy. **Exception, accepted 2026-11-16 (T-45):** on 2026-11-16T08:21Z the previous 14-dump rule deleted `almahmoud-20261026T123937Z.dump.enc` during the T-35 backup rerun; it was a superseded 2026-10-26 snapshot; the tooling has no off-host copy step and a bounded search of this host found none, and the manual off-host step 5 is unconfigured, so the owner accepted the loss as designed retention. Off-host copying remains the manual step 5 until a destination exists.
- **Verified numbers (2026-10-27):** deployment-mode dump 284 kB, backup 2.7 s, drill 8 s, green at migration 0022; host-mode dump 515 kB, green at migration 0022. Both scratch databases were dropped afterwards.
- **Host dependency:** the backup and drill tasks run in the operator's Windows session; the machine must be logged on (or the task configured to run whether or not the user is logged on, which needs the account password). The health watch is meant to be the exception: registered from an elevated shell it runs as SYSTEM at startup and every 15 minutes with no interactive logon. The interactive fallback is opt-in (`-AllowInteractiveFallback`), runs every 15 minutes and at that account's logon, and is refused when the name already belongs to another principal — an unelevated rerun cannot silently downgrade a SYSTEM task. Record which one is live in docs/02-environment.md; on the pilot host it is SYSTEM (registered 2026-11-16T12:48, T-43).
- **The legacy dev drill** (scripts/backup_restore_drill.py, unencrypted, build/backup/) is kept for the phase-1 acceptance evidence; the product path is deploy/backup/*.

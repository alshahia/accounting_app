# Runbook — Windows installation and the offline walkthrough (AC-21, AC-22)

- **Last verified:** 2026-10-27T08:36:00+03:00 by Build agent (installer dry run on the build host: fresh database, setup, sign-in, health, memory sample; evidence in work/release-evidence/2026-10-27-installer/). The **owner run on a clean, network-isolated 2 vCPU / 4 GB VM** is still outstanding and is what satisfies AC-21 and AC-22.
- **Estimated duration:** 10–15 minutes with the installer; 45–60 minutes for the full owner walkthrough including the browser pass and the memory sample
- **Risk:** medium — the walkthrough creates a tenant and an administrator on the target machine; the installer writes real secrets under C:\AlMahmoud
- **Reversible:** yes — `install.bat stop` keeps the data, `docker compose ... down -v` removes the pilot database, and deleting C:\AlMahmoud removes the installation

## Purpose

Install the release candidate on a Windows machine with one double-click, and
produce the evidence for AC-21 (offline installation transcript) and AC-22 (peak
memory on a 2 vCPU / 4 GB machine) from the machine that actually ran it.

## What the installer is

| File | Role |
|---|---|
| deploy/windows/install.bat | double-click entry point; prefers pwsh, falls back to Windows PowerShell, keeps the window open so an error cannot flash past |
| deploy/windows/install.ps1 | the procedure: machine check, secrets and certificate, image load, compose up, health wait, first-run URL, JSON report, friendly error block with the log path |
| deploy/windows/measure-memory.ps1 | samples docker stats for a bounded window and writes a JSON report with the peak |
| deploy/windows/build-offline-bundle.ps1 | (build host) produces images.tar, the deploy tree, manifest.json and SHA256SUMS for the offline media |

Actions: `install.bat` (install or repair), `install.bat check` (machine only),
`install.bat status`, `install.bat logs`, `install.bat stop`, `install.bat start`.
Everything the installer does is written to C:\AlMahmoud\logs\install-<stamp>.log and
summarised in C:\AlMahmoud\logs\install-report-<stamp>.json.

## Build host: produce the offline media

~~~pwsh
docker build -f deploy/Dockerfile -t almahmoud-erp-api:prod .
pwsh -File deploy/windows/build-offline-bundle.ps1
# bundle: build/windows-bundle (images.tar + deploy/ + manifest.json + SHA256SUMS)
~~~

Verify the bundle before copying: the script prints the three image ids and the
size, and writes SHA256SUMS. Copy the whole bundle directory to the target
machine with removable media; nothing on the target needs the internet.

## Target machine

- Windows 11 Pro (Windows 10 21H2 or newer also works), clean, with Docker
  Desktop already installed. **Disable every network adapter before the install
  step** — no registry pull, no docker pull, no package manager.
- 2 vCPU / 4 GB RAM for the AC-22 measurement. A larger machine can run a second
  functional pass, but record which machine produced each number.

## Steps

### 1. Verify the media

~~~pwsh
Get-FileHash C:\almahmoud\images.tar -Algorithm SHA256
# must equal the images.tar line in C:\almahmoud\SHA256SUMS
~~~

### 2. Install with the installer (this is the AC-21 transcript)

Start PowerShell **as administrator** in C:\almahmoud, then either double-click
`deploy\windows\install.bat` or run:

~~~pwsh
Start-Transcript C:\almahmoud\owner-install-transcript.txt
pwsh -File C:\almahmoud\deploy\windows\install.ps1 -Action check
pwsh -File C:\almahmoud\deploy\windows\install.ps1 -Action install
Stop-Transcript
~~~

The installer:

1. checks Windows, Docker Desktop/engine, Compose v2, curl, free ports, disk
   space and the bundle files, and refuses with the exact failed row;
2. copies the deploy tree to C:\AlMahmoud\deploy;
3. generates .env (database password, JWT secret, bootstrap token) and the
   self-signed certificate, and writes C:\AlMahmoud\credentials.txt;
4. loads the images from images.tar when they are not present;
5. starts db, migrate, api and gateway, waits for the TLS health endpoint, and
   prints the URL and the bootstrap token;
6. writes install-report-<stamp>.json with the host, images, ports, setup state
   and health.

If Docker Desktop is installed but idle, the installer starts it and waits. If
an error occurs, it prints what to try, the log path, and exits non-zero; the
batch window stays open. Nothing is deleted on a failed install.

### 3. Provision through the product (no SQL, no seed script)

Open https://127.0.0.1:8443, accept the self-signed certificate warning, type
the bootstrap token from C:\AlMahmoud\credentials.txt, create the tenant, company
and first administrator, then sign in. Create the second user from
Administration → Users → Invite and accept it in a second browser profile.

### 4. AC-22 measurement on this machine

With the stack serving the browser session:

~~~pwsh
pwsh -File C:\AlMahmoud\deploy\windows\measure-memory.ps1 -Minutes 10 -IntervalSeconds 15
# prints the peak summed across db/api/gateway and writes memory-<stamp>.json
Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory
Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors
~~~

Record the peak next to the machine's 4,096 MB and the exact command. Quote the
container limits from the compose file: db 2.0 cpu / 2 GB, api 2.0 cpu / 1 GB,
gateway 1.0 cpu / 256 MB, migrate 1.0 cpu / 512 MB.

### 5. Cleanup

~~~pwsh
C:\AlMahmoud\deploy\windows\install.bat stop      # keeps the database volume
# to remove the pilot data as well:
docker compose -p almahmoud-erp-prod -f C:\AlMahmoud\deploy\docker-compose.prod.yml down -v
# then delete C:\AlMahmoud after moving the transcripts and the reports out
~~~

Rotate or destroy the bootstrap token after the first setup (delete
credentials.txt), and keep .env on the machine only for as long as the
installation lives.

## Manual fallback (if the installer itself fails and cannot be fixed on site)

1. `docker load -i C:\almahmoud\images.tar` — expect three Loaded image lines whose
   ids match manifest.json.
2. Copy the bundle deploy tree to C:\AlMahmoud\deploy.
3. `pwsh -File C:\AlMahmoud\deploy\generate-certs.ps1`.
4. Write C:\AlMahmoud\.env with POSTGRES_PASSWORD, MAHMOUD_JWT_SECRET,
   MAHMOUD_BOOTSTRAP_TOKEN (each at least 32 random bytes), MAHMOUD_HTTP_PORT=8080,
   MAHMOUD_HTTPS_PORT=8443.
5. `docker compose --env-file C:\AlMahmoud\.env -f C:\AlMahmoud\deploy\docker-compose.prod.yml up -d --wait`.
6. Continue from step 3 above.

This is the fallback, not the evidence path: AC-21 wants the installer (or an
equally explicit manual transcript), and either way the transcript, the log and
the browser walkthrough are attached.

## Verification

| Claim | Evidence |
|---|---|
| installation completed | transcript ends with "Installation finished" and the install log has no red block |
| product works | setup status -> needs_setup false, sign-in succeeds, web shell and /api/v1/health return 200 |
| no internet used | network adapters disabled during the whole run; images came from images.tar (manifest ids match) |
| migration at head | migrate log shows 0022_remove_backup_permission, or the report's setup status carries version 0.2.0 |
| memory measured on the target | measure-memory JSON says host_total_mb 4096 (or the recorded VM size) and the peak is quoted with the command |

## Recording

Append to docs/03-handoff.md: date, machine (CPU/RAM), image ids from
manifest.json, install-report path, `docker compose ps` output, setup/login
result, the peak memory number and the transcript path. AC-21 and AC-22 are met
only when the owner's clean-VM transcript and the target-class memory number
exist; this runbook and the build-host dry run do not substitute for them.

## Rollback

1. `install.bat stop` stops the stack and keeps the database volume for a later
   start.
2. `docker compose -p almahmoud-erp-prod -f C:\AlMahmoud\deploy\docker-compose.prod.yml down -v`
   removes the pilot data; take a backup first if the data matters
   (docs/10-runbooks/backup-restore.md).
3. Remove C:\AlMahmoud entirely to remove the secrets and the certificate; rotate the
   bootstrap token and the database password if the machine is being reused.

## Failure modes

| Symptom | Cause | Action |
|---|---|---|
| "The machine is not ready (Docker engine)" | Docker Desktop not installed or still starting | install/start Docker Desktop, wait for "Engine running", run install.bat again |
| "port 8080 is used by ..." | another service or an earlier stack | close that service, or install with -HttpPort 8081 -HttpsPort 8444 |
| "images missing and no images.tar" | ran the script from a source tree without the image | use the offline bundle, or docker build the image on that machine |
| install stops at health, api log shows password authentication failed | the database volume was created with another password | down -v (deletes the pilot database) and install again, or restore the matching .env |
| "docker load" fails | media copy corrupt | re-copy and compare against SHA256SUMS |
| setup returns 403 | bootstrap token in .env differs from the one typed | read the token from C:\AlMahmoud\credentials.txt or .env, type it again |
| gateway returns 502 | API container not healthy | install.bat logs; read the api and migrate lines before restarting |
| certificate warning in the browser | self-signed pilot certificate | expected for the pilot walkthrough; a production install uses the organisation's certificate |

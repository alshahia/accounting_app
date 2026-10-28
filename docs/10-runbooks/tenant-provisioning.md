# Runbook — provision a tenant, company and users

- **Last verified:** 2026-10-26T15:38:00+03:00 by Build agent (fresh install through the production TLS stack: setup 201, second setup 409, login 200; see .local/setup-result.json and .local/login-result.json)
- **Estimated duration:** 5–10 minutes for the first tenant; 2 minutes per additional user
- **Risk:** medium — the first setup creates a tenant, a company and an administrator with full permissions
- **Reversible:** no — the first tenant cannot be removed through the product; delete the database volume to undo a pilot install

## Purpose

Turn a fresh installation into a working product without SQL and without the
demo seed: create the first tenant, company and administrator, then create users
and companies through the administration screen.

## Use when

- A new installation answers GET /api/v1/setup/status with {"needs_setup": true}.
- Adding a company or a user to an existing installation (steps 4–5).
- **Do not** run the setup endpoint more than once; it returns 409 setup_completed
  after the first tenant exists, and that is the intended guard.

## Preconditions

- [ ] The stack is up: docker compose -f deploy/docker-compose.prod.yml ps shows api and gateway healthy.
- [ ] The operator knows the bootstrap token (MAHMOUD_BOOTSTRAP_TOKEN, at least 32 bytes) from the deployment secrets.
- [ ] The TLS certificate is trusted or the browser is allowed to accept the pilot certificate.
- [ ] A password manager is open for the administrator's first password (at least 10 characters and one digit).

## Steps

### 1. Confirm the installation is fresh

~~~pwsh
curl.exe -k -sS https://<host>/api/v1/setup/status
# expect {"needs_setup":true,"bootstrap_configured":true,...}
~~~

### 2. Create the first tenant and administrator

Open https://<host>/ in a browser. The first-run screen asks for tenant name and
slug, company code and names, currency, fiscal year, administrator email, name
and password, the bootstrap token and the MFA policy.

For a scripted install, the equivalent request is:

~~~pwsh
curl.exe -k -sS -X POST -H 'Content-Type: application/json' -H "X-Bootstrap-Token: <token>" --data-binary '@setup.json' https://<host>/api/v1/setup
# expect 201 with tenant_id, company_id, user_id and login_url
~~~

### 3. Sign in as the administrator

Open https://<host>/ and sign in with the administrator email and password. The
first load asks to enrol TOTP when require_mfa is true; keep the recovery codes.

### 4. Create further companies (optional)

Administration -> Companies -> create: code, Arabic name, English name, currency.
The company switcher in the shell shows the new company immediately.

### 5. Invite users

Administration -> Users -> invite: email, full name, company, role
(tenant_admin, company_admin, accountant, sales, purchasing, auditor).
The screen returns a one-time invitation link of the form
https://<host>/?invite=<token>. Send it over the operator's channel, not email
alone; the token expires and is single-use (a second accept returns 409
credential_used).

The invited user opens the link, sets a password (at least 10 characters and one
digit) and signs in. Password resets follow the same one-time link path from the
user list.

### 6. Verify the provisioning

~~~pwsh
# as the administrator, from the browser session:
# - the shell lists the expected companies,
# - Administration -> Roles shows the five default roles and their permissions,
# - Administration -> Users shows every invited account with the right state,
# - the audit log (Administration or /api/v1/audit) shows setup.tenant_provisioned,
#   admin.company.created, admin.user.created and auth.credential_accepted.
~~~

## Verification

- GET /api/v1/setup/status returns needs_setup false for a fresh install after step 2.
- A second POST /setup returns 409 setup_completed.
- The administrator signs in and sees exactly the companies created here.
- Each invited user can sign in once and is refused on replay of the invitation link.

## Rollback

The first tenant cannot be deleted through the product. To abandon a pilot
installation before real data exists:

~~~pwsh
docker compose -f deploy/docker-compose.prod.yml down -v
~~~

**This destroys the database volume.** Never run it after real postings exist; use
backup-restore.md instead. A user created by mistake can be deactivated from
Administration -> Users (the account keeps its audit trail).

## Failure modes

| Symptom | Cause | Action |
|---|---|---|
| 403 setup_not_available | MAHMOUD_BOOTSTRAP_TOKEN is empty in production | set the token in .env, recreate the api service |
| 403 setup_token_invalid | wrong or missing X-Bootstrap-Token | copy the token from the deployment secret; it is compared in constant time |
| 409 setup_completed | a tenant already exists | use the administration screen; the setup screen is not the onboarding path for more tenants |
| 422 weak_password | password under 10 characters or without a digit | choose a stronger password; the rule is enforced by the product |
| invitation link says credential_used | the link was opened twice | issue a password reset from Administration -> Users |
| invited user sees an empty company list | membership was created for a different company | re-invite with the intended company or add the membership |

## Notes

- The demo seed (scripts/seed_demo.py) is a development fixture only; it is not part of this runbook and must never run on a pilot or production database.
- The bootstrap token only guards the first-run endpoint. Once a tenant exists the token is inert for setup and remains only as a deployment secret to rotate.
- Every provisioning action is audited; the audit rows are append-only at the database level (migration 0020).

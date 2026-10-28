# 0002 — Phase-0 dependency licence exceptions

- **Status:** accepted
- **Date:** 2026-10-21T14:05:00+03:00
- **Decided by:** Build lead (engineering decision under the owner's standing direction to continue the app; the owner may reverse it)
- **Supersedes:** none

## Context

The phase-0 licence gate allows only MIT, Apache-2.0, BSD-2/3,
ISC, 0BSD, Unlicense, CC0, PostgreSQL and zlib outright. MPL-2.0 and
LGPL-2.1/3.0 are allowed case-by-case, unmodified only, with a separate decision
record; every other licence fails. The licence scan of the installed phase-0
dependency set found three licences outside the outright allow-list, all in
unmodified third-party distributions:

| Package | Licence | Why it is present | Can it be removed now? |
|---|---|---|---|
| `certifi` | MPL-2.0 | CA bundle used by `httpx` (test-only client) | Not without replacing the test client; it ships no code linked into the app |
| `lightningcss` + platform binding | MPL-2.0 | Vite 8 CSS transformer, build-time dev dependency | Vite can fall back to a different CSS pipeline, but not in phase 0 |
| `python-bidi` | LGPL-3.0 | Arabic PDF visual ordering in `app/modules/reports/render.py`; its `COPYING.LESSER` in 0.6.12 is LGPL-3.0 | Could be replaced by an in-repo bidi pass, but that is a correctness risk for mixed Arabic/Latin/number lines and is not a phase-0 task |
| `greenlet`, `typing_extensions` | PSF-2.0 | SQLAlchemy async runtime and Python typing backport; `greenlet` is also part of the SQLAlchemy licence expression | No: they are transitive runtime requirements of the approved stack |
| `defusedxml` | PSF-2.0 | Transitive dependency of the dev-only `pip-audit` dependency audit tool | No: the audit tool is part of the day-10 guardrail and is not shipped |

None of these packages is modified, vendored into our source, statically linked
or redistributed as part of our own code. The LGPL package is used as an
unmodified Python library and imported at runtime; the MPL packages are a CA
bundle and a build-time tool. No GPL, AGPL, SSPL, BSL, Elastic, FSL,
source-available or commercial licence appears in the phase-0 Python or Node
dependency set.

## Decision

Accept MPL-2.0, LGPL-3.0 and PSF-2.0 as **phase-0 exceptions** for the specific
unmodified packages listed above, and teach `scripts/check_licences.py` to allow
exactly these licences only while this record exists. The SBOM and the
third-party notices file list every package and licence, and the gate prints the
exceptions it used on every run.

## Alternatives considered

| Alternative | Why it was not chosen |
|---|---|
| **Remove `python-bidi` and hand-roll bidi ordering** | A small incorrect bidi pass silently corrupts Arabic PDFs; the risk is higher than an unmodified LGPL library. Revisit when a maintained permissive bidi implementation is chosen. |
| **Replace `httpx`/`certifi` with a client that bundles no CA store** | The HTTP client is test-only; the exception is lower-risk than changing the test harness to avoid a data file. |
| **Pin Vite 7 / drop `lightningcss`** | Build tooling only; downgrading Vite to dodge a dev-only MPL dependency is not worth the churn in phase 0. |
| **Treat PSF-2.0 as implicitly allowed** | The allow-list is explicit; silently widening it would weaken the gate. A named exception is honest and reviewable. |

## Consequences

**Positive**

- The phase-0 dependency set ships with no banned licence and no hidden
  exception; every exception is named, versioned and visible in the SBOM.
- The gate fails closed: if a future dependency uses MPL-2.0 or LGPL-3.0 without
  this file, the check fails.

**Negative / accepted costs**

- Phase 0 carries one LGPL-3.0 runtime library and two MPL-2.0 build/test
  dependencies; redistribution (if we ever ship the Python environment) must
  keep their notices and allow relinking/replacement as the licences require.
- The exception list is code-adjacent policy; a future reviewer must re-open this
  record when a package is upgraded or replaced.

## Reversibility

Easy for the MPL build/test packages and `certifi` (swap the dependency) and
moderate for `python-bidi` (replace the renderer's bidi step and re-run the PDF
tests and the visual review). Removing an exception means editing
`scripts/check_licences.py` and this record together.

## Follow-up

- [ ] **Owner review** — confirm these exceptions at the phase-0 exit review;
  owner: user.
- [ ] **Phase 1 licence review** — re-scan after every dependency addition and at
  each release; owner: build lead.
- [ ] **Notices** — keep `THIRD_PARTY_NOTICES.md` regenerated from the SBOM
  script in CI; owner: build lead.

# NETE v1.0.0 — LAB prototype

Release date: 3 November 2026. The owner approved publication to the existing public repository. This release uses the verified clean public base and sanitized project snapshot, excluding the three credential-bearing local commits.

## Scope

The first proposed NETE release documents the existing working Network Diagnosis and Email Proactiveness workspaces, read-only MikroTik diagnostics, synthetic site matching, SQLite incident management, diagnostic evidence, editable email drafts and the separate Prometheus/Grafana monitoring path. It preserves the existing flat module structure and diagnostic rules.

## Stabilization

- Stable case-specific transcript/JSON download keys avoid collisions when identical saved reports are displayed together.
- Opening the current case from the saved queue renders its report and draft once, avoiding duplicate report/draft widget IDs.
- New Network Diagnosis attempts clear old results before validation or SSH, including candidate/custom-IP checks. Failed authentication or timeouts cannot leave an old success displayed as the new attempt.
- Email diagnostic exception messages redact current connection credentials before display or SQLite persistence.
- Exporter failures avoid logging raw exception tracebacks that could include credentials. Metric collection logic is retained.
- Obsolete commented connection examples use safe placeholders; ignored backups and snapshots remain private locally.
- Git exclusions cover local secrets, SQLite files, backups, snapshots, key files, caches and common export folders.
- Regression tests exercise failure handling, corrected-credential retry, duplicate download rendering, active/saved case rendering, and credential-bearing errors.

## Existing incident safeguards included

Every Scan email click starts fresh triage without SSH and requires renewed site confirmation. Fresh investigation IDs prevent an identical email from reusing historical success as a new diagnostic attempt. Saved synthetic LAB cases can be removed only through a separate case-specific confirmation. Other SQLite records are retained. Link-down/restoration investigations retain 10-ping recommendations; degraded/intermittent investigations retain 100, subject to unchanged early-stop rules.

## Security and limitations

This is a working LAB prototype and engineering portfolio project. SSH server identity is not verified by the current connection defaults. There is no app authentication, per-user case isolation, encrypted case store, production target allowlist in Network Diagnosis or formal compliance assessment. Synthetic labels do not establish real network isolation. Exported evidence and pasted incident text require review before sharing.

The published repository history checked did not contain the LAB password. It appears only in three later local commits, which are excluded from this release's ancestry. The existing password and original local history are preserved; no force push or router credential change is part of publication. Private configuration, case databases, backups and snapshots are excluded.

## Verification

The final release snapshot passes 234 automated tests and 50 subtests using mocked SSH, synthetic evidence and temporary databases. The final scan checks all release files and ancestry for the known local credential and common secret/private-key patterns. No production devices or real credentials were used in testing.

# Land Tracker Reliability Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement and verify each task in order.

**Goal:** Recover from temporary source failures and interrupted collections without publishing incomplete or falsely fresh data.

**Architecture:** Keep the existing parsers and all-or-nothing publication gate. Add bounded transaction retries, same-day validated HTTP checkpoints for enrichment, and a checked Git rebase for unrelated remote changes. GitHub Actions supplies two daily recovery windows and persists checkpoints even after collection failure.

**Tech Stack:** Python standard library, existing requests/BeautifulSoup, unittest, GitHub Actions.

**Spec:** The user's approved fixes in this thread: automatic recovery, checkpoint continuation, and publication conflict handling.

## Global Constraints

- Never overwrite manual Base remarks or clear optional data with blanks.
- Checkpoints expire after six hours or at Shanghai midnight, whichever comes first.
- Only responses from a fully validated query may be checkpointed; all reused responses are parsed and matched again.
- Preserve the oldest reused observation time; never label yesterday's response as today's collection.
- No force push, arbitrary merge resolution, new credentials, or changes to the native Feishu workflow.

## Review Focus

- Interrupted or malformed checkpoint files must cause a fresh fetch.
- A partially failed multi-page query must not become a successful checkpoint.
- Same-day successful runs should skip scheduled recovery, but explicit/push runs still validate changes.
- Concurrent changes to generated data or collector code must stop publication, not overwrite the newer version.
- A push rejected after an initial fetch must fetch and check the remote again.

## Tasks

- [x] Add failing tests in `scripts/tests/test_land_tracker_recovery.py`, `test_land_tracker_checkpoint.py`, and `test_land_tracker_publish.py`, plus checkpoint-feed tests. Observed 14 failures and one error before implementation; the existing 61 tests remained green.
- [x] Add `scripts/land_tracker_checkpoint.py`; integrate query transactions with `PublicClient`, `Collector.attempt`, identity collection, and evidence timestamps. Run checkpoint and existing enrichment tests.
- [x] Add `scripts/land_tracker_recovery.py`; classify transport failures in the transaction collector, retry only transient failures after 60/180 seconds, and implement the scheduled same-day freshness gate. Run recovery tests.
- [x] Add `scripts/commit_land_tracker.py`; test with real temporary bare repositories that unrelated commits survive and protected changes block publication, including a push race after fetching.
- [x] Update workflow: 08:30 primary, 11:17/14:17 recovery, 150-minute job budget, 120-minute enrichment budget, same-day code-versioned cache restore/save, and safe publisher. Update operating documentation. Same-day skips still verify/rebuild Pages so a previous deployment failure can recover.
- [x] Run local release checks: 118 repository unit tests passed, JavaScript rendering passed, desktop/mobile browser regression passed (76 rows), workflow YAML parsed and guards checked, and `git diff --check` passed. Live transaction dry run read 44 official entries, matched 28 residential entries, and kept 76 rows with no additions or changes.
- [ ] Publish scoped changes and verify GitHub run/public deployment status. Full official permit recollection is a separate cloud run; local transaction verification does not establish its success.

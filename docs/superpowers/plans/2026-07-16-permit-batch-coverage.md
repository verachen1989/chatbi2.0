# 预售证批次级库存闭合 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将住建委库存抓取从项目级汇总改为预售证批次级闭合，避免嘉棠璟樾等多证项目漏抓一批后被误判为完整。

**Architecture:** 每个项目先拆成独立的 permit batches；每批次独立验证详情页、楼栋批准套数和房源状态，再把批次结果汇总到项目级三层门禁。任意批次缺证据、空壳或不闭合，项目保持 `unavailable`/`mismatch`，正式快照和 dashboard 不更新。历史楼栋发现只作为审计线索，不能跨预售证补齐正式库存。

**Tech Stack:** Python 3.12、标准库 `unittest`、现有 HTML/JSON 解析器、现有住建委请求和快照写入流程。

## Global Constraints

- 不写入未经所有预售证批次闭合的正式库存。
- 不用楼号或楼栋 ID 数学推断生成正式楼栋证据。
- 页面空壳、网络异常和结构缺证据必须进入失败审计；不写入 0。
- 保持 `coverageStatus=complete|partial|mismatch|unavailable|legacy` 兼容。
- 既有快照中未通过新版门禁的旧值继续保留，并标记 `legacy`。

---

### Task 1: 定义批次证据模型和门禁辅助函数

**Files:**
- Modify: `scripts/update_zjw_inventory_status.py`
- Test: `scripts/tests/test_update_zjw_inventory_status.py`

**Interfaces:**
- Produces `build_permit_batches(item, urls, page_evidence)`、`classify_permit_batch(...)`、`aggregate_permit_batches(...)`。
- `permitCoverage` 每项至少包含 `permit`、`issueDate`、`detailUrl`、`detailStatus`、`approvedSuites`、`roomStatusTotal`、`unknown`、`coverageStatus`、`error`。

- [x] **Step 1: Write failing tests** for two permits with one small batch and one missing batch, reversed URL order, and duplicate building keys across batches.
- [x] **Step 2: Run `python3 -m unittest scripts.tests.test_update_zjw_inventory_status -v` and confirm the new tests fail because the helpers do not exist.
- [x] **Step 3: Implement minimal batch normalization and aggregation helpers.** Map permits by page evidence/permit identity, never by list position alone; reject unresolved permit-to-URL mappings.
- [x] **Step 4: Run the focused tests and confirm they pass.**
- [x] **Step 5: Keep the change isolated; do not change dashboard writing in this task.

### Task 2: Refactor `scrape_project` to fetch and close each permit batch

**Files:**
- Modify: `scripts/update_zjw_inventory_status.py`
- Test: `scripts/tests/test_update_zjw_inventory_status.py`

**Interfaces:**
- `scrape_project` returns project-level totals plus `permitCoverage`.
- Detail-page retries are keyed by permit batch; a successful batch is cached independently from a failed batch.

- [x] **Step 1: Add a failing fixture test** where `2026(53)` has 3 available suites and `2026(5)` has the main buildings; one detail URL returns a shell. Assert project status is not `complete` and no formal result is accepted.
- [x] **Step 2: Run the fixture test and verify it fails against the current project-level implementation.
- [x] **Step 3: Refactor detail fetch state** from `completed_detail_urls`/shared cache to per-batch state, preserving source URL, permit number, issue date, and batch-specific rows.
- [x] **Step 4: Apply per-batch G1/G2 gates; only aggregate batches whose evidence is valid, and mark missing batches explicitly.
- [x] **Step 5: Run the fixture test and all existing script tests.

### Task 3: Restrict hidden-building discovery and improve failure audit

**Files:**
- Modify: `scripts/update_zjw_inventory_status.py`
- Test: `scripts/tests/test_update_zjw_inventory_status.py`

**Interfaces:**
- Discovery receives a single permit batch and its expected remaining total.
- Failure records include `missingPermitBatches` and per-batch error details.

- [x] **Step 1: Add failing tests** for a missing batch and for an ID-range discovery that must not turn a project into `complete`.
- [x] **Step 2: Run the tests and confirm the current project-wide discovery incorrectly has no batch-level failure information.
- [x] **Step 3: Make discovery batch-scoped and audit-only; it may produce candidates for review but cannot satisfy G1 without an anchored valid detail/list source.
- [x] **Step 4: Add `permitCoverage` to `project_snapshot`, partial failures, and history latest records.
- [x] **Step 5: Run focused tests and validate legacy snapshot migration still preserves old values.

### Task 4: End-to-end validation and release

**Files:**
- Modify: `scripts/tests/test_update_zjw_inventory_status.py` only if a regression assertion is missing.
- Generated: `data/zjw_inventory_snapshot.json`, `data/zjw_inventory_history.json`, `data/zjw_inventory_watchlist.json` only if the approved test run produces safe changes.

- [x] **Step 1: Run `python3 -m unittest discover -s scripts/tests -p 'test_*.py' -v`.
- [x] **Step 2: Run a single-project dry/full test for `嘉棠璟樾` and inspect both permit batches.
- [x] **Step 3: Run `python3 -m py_compile scripts/update_zjw_inventory_status.py` and `git diff --check`.
- [x] **Step 4: Confirm no incomplete project updates dashboard or formal inventory.
- [ ] **Step 5: Stage only the scraper, tests, and explicitly generated inventory files; commit with `Update ZJW inventory status` and push `origin main`.

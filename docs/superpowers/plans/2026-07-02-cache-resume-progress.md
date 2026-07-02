# Cache Resume Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add persistent blob cache, HTTP Range resume, retry after validation failures, and stderr download progress to `docker_blob_downloader.py`.

**Architecture:** Keep the project as a standard-library Python CLI. Store verified blobs under a digest-addressed cache directory, keep `.part` files for resume, add `Range` requests when a partial file exists, retry whole-blob downloads on network, digest, and size failures, and report progress through a small stderr reporter that is easy to test.

**Tech Stack:** Python 3 standard library, `unittest`, Git.

## Global Constraints

- Preserve default image `quay.io/ascend/vllm-ascend:deepseekv4-a3`.
- Preserve default architecture input `aarch64`.
- Keep support for `--http-proxy`, `--https-proxy`, environment proxy variables, and `--insecure`.
- No silent error handling; failures must exit non-zero with a clear message.
- Do not add runtime dependencies.

---

### Task 1: Cache and Resume Paths

**Files:**
- Modify: `docker_blob_downloader.py`
- Modify: `tests/test_docker_blob_downloader.py`

**Interfaces:**
- Produces: `default_cache_dir() -> pathlib.Path`
- Produces: `cache_blob_path(cache_dir: pathlib.Path, digest: str) -> pathlib.Path`
- Produces: `cache_part_path(cache_dir: pathlib.Path, digest: str) -> pathlib.Path`

- [x] Write failing tests that cache paths are digest-addressed and partial paths end in `.part`.
- [x] Implement cache path helpers and CLI options `--cache-dir` and `--no-cache`.
- [x] Run `python3 -m unittest discover -s tests -v`.

### Task 2: Retry and Resume Downloads

**Files:**
- Modify: `docker_blob_downloader.py`
- Modify: `tests/test_docker_blob_downloader.py`

**Interfaces:**
- Produces: `RegistryClient.download_blob(descriptor, dest_dir, cache_dir=None, progress=None) -> pathlib.Path`
- Produces: `RegistryClient.download_blob_once(descriptor, dest_path, part_path, progress=None) -> pathlib.Path`

- [x] Write failing tests that `download_blob` retries after a digest mismatch and that resume requests include `Range: bytes=<partial-size>-`.
- [x] Implement retry around network, digest, and size validation failures.
- [x] Implement `.part` resume using HTTP `Range`.
- [x] Run `python3 -m unittest discover -s tests -v`.

### Task 3: Progress Reporting and Docs

**Files:**
- Modify: `docker_blob_downloader.py`
- Modify: `tests/test_docker_blob_downloader.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `ProgressReporter` with `start_blob`, `update`, and `finish_blob`.

- [x] Write failing tests that progress output includes percentage and bytes.
- [x] Implement stderr progress output enabled by default when downloading.
- [x] Add `--no-progress`.
- [x] Update README for cache, resume, validation retry, and progress.
- [x] Run unit tests, CLI help, and live dry-run.

### Task 4: Commit and Push

**Files:**
- Modify: Git branch metadata.

- [x] Run final verification commands.
- [ ] Commit the feature.
- [ ] Push `feature/cache-resume-progress` to GitHub.

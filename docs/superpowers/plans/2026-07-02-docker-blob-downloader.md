# Docker Blob Downloader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and publish a public GitHub repository with a Python script that downloads Docker image blobs and assembles an OCI archive.

**Architecture:** A standard-library Python CLI handles registry API calls, platform selection, proxy/TLS settings, blob verification, and OCI archive creation. Unit tests cover parsing, platform matching, digest verification, and error reporting.

**Tech Stack:** Python 3 standard library, `unittest`, Git, GitHub CLI.

## Global Constraints

- Default image is `quay.io/ascend/vllm-ascend:deepseekv4-a3`.
- Default architecture input is `aarch64`, normalized to Docker registry architecture `arm64`.
- Support `--http-proxy`, `--https-proxy`, standard proxy environment variables, and `--insecure`.
- No silent error handling; failures must exit non-zero with a clear message.

---

### Task 1: Tests and CLI Behavior

**Files:**
- Create: `tests/test_docker_blob_downloader.py`
- Create: `docker_blob_downloader.py`

**Interfaces:**
- Produces: `parse_image_reference(image: str) -> ImageReference`
- Produces: `normalize_arch(arch: str) -> str`
- Produces: `select_platform_manifest(index: dict, os_name: str, arch: str, variant: str | None) -> dict`
- Produces: `verify_digest(path: pathlib.Path, digest: str, expected_size: int | None = None) -> None`

- [x] Write failing tests for default parsing, architecture normalization, platform selection, and digest failure.
- [x] Run `python3 -m unittest discover -s tests -v` and confirm tests fail because implementation is missing.
- [x] Implement the minimal script functions and CLI constants.
- [x] Run `python3 -m unittest discover -s tests -v` and confirm tests pass.

### Task 2: Registry Download and OCI Assembly

**Files:**
- Modify: `docker_blob_downloader.py`
- Create: `README.md`
- Create: `.gitignore`

**Interfaces:**
- Produces: `RegistryClient` with manifest and blob download methods.
- Produces: `write_oci_archive(...) -> None`
- Produces: CLI options for image, arch, os, proxies, insecure TLS, dry run, output, retries, timeout, and verbose errors.

- [x] Implement Registry API v2 token authentication, manifest retrieval, blob download, digest verification, and archive writing.
- [x] Write README usage examples for the default image, proxy options, `--insecure`, dry run, and loading the archive.
- [x] Run unit tests.
- [x] Run CLI help.
- [x] Run a dry run against the default image to verify live manifest resolution without downloading layers.

### Task 3: GitHub Publication

**Files:**
- Modify: repository metadata through Git and GitHub CLI.

- [ ] Initialize Git.
- [ ] Commit project files with the configured user identity.
- [ ] Create `370025263/docker-blob-downloader` as a public repository.
- [ ] Push the default branch.
- [ ] Verify remote URL and public visibility.

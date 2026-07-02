# Docker Blob Downloader Design

## Goal

Build a public GitHub repository containing a script that downloads a Docker image by registry blobs and assembles those blobs into an OCI image archive. The default image is `quay.io/ascend/vllm-ascend:deepseekv4-a3`; the default architecture input is `aarch64`.

## Requirements

- Provide a command-line script with a positional image argument.
- Default image: `quay.io/ascend/vllm-ascend:deepseekv4-a3`.
- Default architecture: `aarch64`.
- Normalize architecture aliases before matching registry platform metadata. `aarch64` maps to `arm64`; `x86_64` maps to `amd64`.
- Use the Docker Registry HTTP API v2 to resolve manifests, choose the requested platform from a manifest list or OCI index, download config and layer blobs, verify blob digests, and write an OCI archive tar.
- Support `--http-proxy` and `--https-proxy`, while also honoring standard proxy environment variables by default.
- Support `--insecure` to disable TLS certificate validation for environments with HTTPS interception.
- Do not silently ignore errors. Network, authentication, digest, size, manifest, and filesystem failures must produce a non-zero exit and an actionable stderr message.

## Architecture

The script is a single Python file using only the standard library. It has small functions for image reference parsing, architecture normalization, registry authentication, manifest selection, blob verification, and archive assembly. Tests use `unittest` so the repository has no runtime or test dependencies.

## Error Handling

All expected failures raise `RegistryError` with context. The CLI catches exceptions, prints the error to stderr, and exits with status 1. With `--verbose`, it prints the traceback before exiting.

## Verification

Unit tests cover pure behavior without downloading large images. A dry-run mode resolves manifests and lists blobs without writing the archive, allowing live registry checks without downloading every layer.

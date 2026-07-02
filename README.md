# docker-blob-downloader

Download a Docker image from registry blobs and assemble an OCI image archive.

The script defaults to:

- Image: `quay.io/ascend/vllm-ascend:deepseekv4-a3`
- Architecture input: `aarch64`
- OS: `linux`

`aarch64` is normalized to the Docker registry architecture name `arm64`.

## Usage

Resolve the default image and list blobs without downloading layers:

```bash
python3 docker_blob_downloader.py --dry-run
```

Download the default image:

```bash
python3 docker_blob_downloader.py
```

By default, verified blobs are cached under `~/.cache/docker-blob-downloader`.
If the process is interrupted, the next run resumes from any remaining `.part`
file when the registry supports HTTP Range requests.

Download another image:

```bash
python3 docker_blob_downloader.py quay.io/ascend/vllm-ascend:deepseekv4-a3
```

Choose the platform:

```bash
python3 docker_blob_downloader.py --arch aarch64 --os linux
python3 docker_blob_downloader.py --arch amd64 --os linux
```

Choose an output file:

```bash
python3 docker_blob_downloader.py -o vllm-ascend-arm64.tar
```

Choose a cache directory:

```bash
python3 docker_blob_downloader.py --cache-dir /data/docker-blob-cache
```

Disable persistent cache:

```bash
python3 docker_blob_downloader.py --no-cache
```

Load the archive:

```bash
docker load -i vllm-ascend-arm64.tar
```

Modern Docker and containerd tools understand OCI archives. If your Docker
version cannot load this archive, use `skopeo copy oci-archive:vllm-ascend-arm64.tar docker-daemon:quay.io/ascend/vllm-ascend:deepseekv4-a3`.

## Proxy and TLS Options

The script honors standard proxy environment variables by default:

```bash
HTTP_PROXY=http://127.0.0.1:7890 \
HTTPS_PROXY=http://127.0.0.1:7890 \
python3 docker_blob_downloader.py
```

You can also pass proxies explicitly:

```bash
python3 docker_blob_downloader.py \
  --http-proxy http://127.0.0.1:7890 \
  --https-proxy http://127.0.0.1:7890
```

To ignore environment proxy settings:

```bash
python3 docker_blob_downloader.py --no-env-proxy
```

If your network replaces HTTPS certificates and Python rejects the registry
certificate, use:

```bash
python3 docker_blob_downloader.py --insecure
```

`--insecure` disables TLS certificate validation for registry and token
requests. Use it only on a network you understand.

## Error Handling

The script does not silently ignore failures. HTTP errors, authentication
failures, missing platform manifests, digest mismatches, size mismatches, and
filesystem errors print an `ERROR:` message to stderr and exit non-zero.

`--retries` covers request failures, interrupted blob downloads, and digest or
size validation failures. On digest or size mismatch, the bad partial file is
deleted before retrying.

Use `--verbose` to print a traceback:

```bash
python3 docker_blob_downloader.py --verbose --dry-run
```

## Progress

Downloads show a stderr progress line for each config or layer blob:

```text
blob 2/18 sha256:c36472b34583 42.3% 11.0 MiB/26.1 MiB
```

Disable progress output:

```bash
python3 docker_blob_downloader.py --no-progress
```

## Development

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

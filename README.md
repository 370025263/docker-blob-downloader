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

Use `--verbose` to print a traceback:

```bash
python3 docker_blob_downloader.py --verbose --dry-run
```

## Development

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

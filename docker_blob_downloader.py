#!/usr/bin/env python3
"""Download Docker registry blobs and assemble an OCI image archive."""

from __future__ import print_function

import argparse
import hashlib
import io
import json
import os
import re
import ssl
import sys
import tarfile
import tempfile
import time
import traceback
from pathlib import Path

try:
    from urllib import error as urlerror
    from urllib import parse as urlparse
    from urllib import request as urlrequest
except ImportError:  # pragma: no cover
    import urllib2 as urlrequest
    import urllib as urlparse
    import urllib2 as urlerror


DEFAULT_IMAGE = "quay.io/ascend/vllm-ascend:deepseekv4-a3"
DEFAULT_ARCH = "aarch64"
DEFAULT_OS = "linux"
DEFAULT_CACHE_NAME = "docker-blob-downloader"

OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
DOCKER_MANIFEST_LIST_MEDIA_TYPE = (
    "application/vnd.docker.distribution.manifest.list.v2+json"
)
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
DOCKER_MANIFEST_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v2+json"

INDEX_MEDIA_TYPES = set([OCI_INDEX_MEDIA_TYPE, DOCKER_MANIFEST_LIST_MEDIA_TYPE])
IMAGE_MANIFEST_MEDIA_TYPES = set([OCI_MANIFEST_MEDIA_TYPE, DOCKER_MANIFEST_MEDIA_TYPE])
MANIFEST_ACCEPT = ", ".join(
    [
        OCI_INDEX_MEDIA_TYPE,
        DOCKER_MANIFEST_LIST_MEDIA_TYPE,
        OCI_MANIFEST_MEDIA_TYPE,
        DOCKER_MANIFEST_MEDIA_TYPE,
    ]
)
IMAGE_MANIFEST_ACCEPT = ", ".join([OCI_MANIFEST_MEDIA_TYPE, DOCKER_MANIFEST_MEDIA_TYPE])


class RegistryError(Exception):
    """Raised for expected registry, archive, and validation failures."""


class ImageReference(object):
    def __init__(self, registry, repository, reference, original, is_digest):
        self.registry = registry
        self.repository = repository
        self.reference = reference
        self.original = original
        self.is_digest = is_digest

    def repo_tag(self):
        if self.is_digest:
            return None
        return "%s/%s:%s" % (self.registry, self.repository, self.reference)


def parse_image_reference(image):
    image = (image or "").strip()
    if not image:
        raise RegistryError("image reference is empty")
    if "://" in image:
        raise RegistryError("image reference must not include a URL scheme: %s" % image)

    original = image
    is_digest = False
    if "@" in image:
        name, reference = image.rsplit("@", 1)
        is_digest = True
        if not reference:
            raise RegistryError("image digest is empty: %s" % image)
    else:
        last_slash = image.rfind("/")
        last_colon = image.rfind(":")
        if last_colon > last_slash:
            name = image[:last_colon]
            reference = image[last_colon + 1 :]
            if not reference:
                raise RegistryError("image tag is empty: %s" % image)
        else:
            name = image
            reference = "latest"

    if not name:
        raise RegistryError("image name is empty: %s" % original)

    parts = name.split("/")
    first = parts[0]
    if len(parts) == 1:
        registry = "registry-1.docker.io"
        repository = "library/" + parts[0]
    elif "." in first or ":" in first or first == "localhost":
        registry = first
        repository = "/".join(parts[1:])
    else:
        registry = "registry-1.docker.io"
        repository = name

    if registry in ("docker.io", "index.docker.io"):
        registry = "registry-1.docker.io"

    if not repository or repository.startswith("/") or repository.endswith("/"):
        raise RegistryError("invalid repository in image reference: %s" % original)

    return ImageReference(registry, repository, reference, original, is_digest)


def normalize_arch(arch):
    value = (arch or "").strip().lower()
    aliases = {
        "aarch64": "arm64",
        "arm64v8": "arm64",
        "x86_64": "amd64",
        "x64": "amd64",
    }
    return aliases.get(value, value)


def platform_string(platform):
    os_name = platform.get("os", "unknown")
    arch = platform.get("architecture", "unknown")
    variant = platform.get("variant")
    if variant:
        return "%s/%s/%s" % (os_name, arch, variant)
    return "%s/%s" % (os_name, arch)


def select_platform_manifest(index, os_name, arch, variant):
    manifests = index.get("manifests") or []
    target_os = (os_name or DEFAULT_OS).lower()
    target_arch = normalize_arch(arch or DEFAULT_ARCH)
    available = []

    for descriptor in manifests:
        platform = descriptor.get("platform") or {}
        if platform:
            available.append(platform_string(platform))
        descriptor_os = (platform.get("os") or "").lower()
        descriptor_arch = normalize_arch(platform.get("architecture") or "")
        descriptor_variant = platform.get("variant")
        if descriptor_os != target_os or descriptor_arch != target_arch:
            continue
        if variant and descriptor_variant != variant:
            continue
        return descriptor

    available_text = ", ".join(available) if available else "none"
    wanted = "%s/%s" % (target_os, target_arch)
    if variant:
        wanted += "/" + variant
    raise RegistryError(
        "no manifest for platform %s; available platforms: %s"
        % (wanted, available_text)
    )


def digest_parts(digest):
    if ":" not in digest:
        raise RegistryError("invalid digest, expected algorithm:hex: %s" % digest)
    algorithm, hex_digest = digest.split(":", 1)
    if algorithm != "sha256":
        raise RegistryError("unsupported digest algorithm %s in %s" % (algorithm, digest))
    if not re.match(r"^[0-9a-fA-F]{64}$", hex_digest):
        raise RegistryError("invalid sha256 digest: %s" % digest)
    return algorithm, hex_digest.lower()


def compute_sha256_digest_bytes(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def verify_digest(path, digest, expected_size=None):
    algorithm, expected_hex = digest_parts(digest)
    hasher = hashlib.new(algorithm)
    size = 0
    with open(str(path), "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            hasher.update(chunk)

    if expected_size is not None and size != expected_size:
        raise RegistryError(
            "size mismatch for %s: expected %s bytes, got %s bytes"
            % (path, expected_size, size)
        )
    actual = hasher.hexdigest()
    if actual != expected_hex:
        raise RegistryError(
            "digest mismatch for %s: expected sha256:%s, got sha256:%s"
            % (path, expected_hex, actual)
        )


def default_cache_dir():
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home).expanduser() / DEFAULT_CACHE_NAME
    return Path.home() / ".cache" / DEFAULT_CACHE_NAME


def digest_blob_path(digest):
    algorithm, hex_digest = digest_parts(digest)
    return "blobs/%s/%s" % (algorithm, hex_digest)


def cache_blob_path(cache_dir, digest):
    algorithm, hex_digest = digest_parts(digest)
    return Path(cache_dir).expanduser() / "blobs" / algorithm / hex_digest


def cache_part_path(cache_dir, digest):
    return Path(str(cache_blob_path(cache_dir, digest)) + ".part")


def is_validation_error(exc):
    text = str(exc)
    return "digest mismatch" in text or "size mismatch" in text


def response_code(response):
    getcode = getattr(response, "getcode", None)
    if getcode is None:
        return None
    try:
        return getcode()
    except Exception:
        return None


def format_bytes(value):
    if value is None:
        return "unknown"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            if unit == "B":
                return "%d B" % int(number)
            return "%.1f %s" % (number, unit)
        number /= 1024.0
    return "%d B" % value


def format_progress_bytes(current, total):
    if total is not None and current < 1024 and total < 1024:
        return "%d/%d B" % (current, total)
    if total is not None:
        return "%s/%s" % (format_bytes(current), format_bytes(total))
    return format_bytes(current)


class ProgressReporter(object):
    def __init__(self, stream=None, enabled=True):
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled
        self.index = 0
        self.total = 0
        self.digest = ""
        self.expected_size = None
        self.current = 0
        self.started_at = None

    def start_blob(self, index, total, digest, expected_size):
        self.index = index
        self.total = total
        self.digest = digest
        self.expected_size = expected_size
        self.current = 0
        self.started_at = time.time()
        self._write()

    def reset_current(self):
        self.current = 0
        self.started_at = time.time()
        self._write()

    def update(self, size):
        self.current += size
        self._write()

    def finish_blob(self):
        self._write(final=True)

    def _write(self, final=False):
        if not self.enabled:
            return
        digest_short = self.digest
        if digest_short.startswith("sha256:"):
            digest_short = "sha256:" + digest_short.split(":", 1)[1][:12]
        if self.expected_size:
            percent = min((float(self.current) / float(self.expected_size)) * 100.0, 100.0)
            size_text = format_progress_bytes(self.current, self.expected_size)
            message = "blob %s/%s %s %.1f%% %s" % (
                self.index,
                self.total,
                digest_short,
                percent,
                size_text,
            )
        else:
            message = "blob %s/%s %s %s" % (
                self.index,
                self.total,
                digest_short,
                format_bytes(self.current),
            )
        if final:
            self.stream.write("\r%s\n" % message)
        else:
            self.stream.write("\r%s" % message)
        flush = getattr(self.stream, "flush", None)
        if flush is not None:
            flush()


def _tar_add_bytes(tar, name, data):
    encoded = data if isinstance(data, bytes) else data.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(encoded)
    info.mode = 0o644
    info.mtime = 0
    tar.addfile(info, io.BytesIO(encoded))


def _tar_add_dir(tar, name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    info.mtime = 0
    tar.addfile(info)


def _tar_add_file(tar, src_path, name):
    src_path = Path(src_path)
    info = tarfile.TarInfo(name)
    info.size = src_path.stat().st_size
    info.mode = 0o644
    info.mtime = 0
    with open(str(src_path), "rb") as handle:
        tar.addfile(info, handle)


def write_oci_archive(
    output_path,
    repo_tag,
    manifest_bytes,
    manifest_digest,
    manifest_media_type,
    config_path,
    layer_paths,
    annotations=None,
):
    output_path = Path(output_path)
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    config_descriptor = manifest.get("config") or {}
    layer_descriptors = manifest.get("layers") or []
    if len(layer_descriptors) != len(layer_paths):
        raise RegistryError(
            "layer count mismatch while writing archive: manifest has %s, files has %s"
            % (len(layer_descriptors), len(layer_paths))
        )

    index_descriptor = {
        "mediaType": manifest_media_type or manifest.get("mediaType") or OCI_MANIFEST_MEDIA_TYPE,
        "digest": manifest_digest,
        "size": len(manifest_bytes),
    }
    if annotations:
        index_descriptor["annotations"] = annotations

    index = {"schemaVersion": 2, "manifests": [index_descriptor]}
    docker_manifest = [
        {
            "Config": digest_blob_path(config_descriptor["digest"]),
            "RepoTags": [repo_tag] if repo_tag else [],
            "Layers": [digest_blob_path(layer["digest"]) for layer in layer_descriptors],
        }
    ]

    tmp_output = output_path.with_name(output_path.name + ".tmp")
    if tmp_output.exists():
        tmp_output.unlink()
    with tarfile.open(str(tmp_output), "w") as tar:
        _tar_add_dir(tar, "blobs")
        _tar_add_dir(tar, "blobs/sha256")
        _tar_add_bytes(tar, "oci-layout", json.dumps({"imageLayoutVersion": "1.0.0"}))
        _tar_add_bytes(tar, "index.json", json.dumps(index, sort_keys=True, indent=2))
        _tar_add_bytes(
            tar, "manifest.json", json.dumps(docker_manifest, sort_keys=True, indent=2)
        )
        _tar_add_file(tar, config_path, digest_blob_path(config_descriptor["digest"]))
        _tar_add_bytes(tar, digest_blob_path(manifest_digest), manifest_bytes)
        for layer_path, layer in zip(layer_paths, layer_descriptors):
            _tar_add_file(tar, layer_path, digest_blob_path(layer["digest"]))
    tmp_output.replace(output_path)


def parse_www_authenticate(header):
    if not header:
        raise RegistryError("registry requested authentication without WWW-Authenticate")
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise RegistryError("unsupported authentication challenge: %s" % header)
    params = {}
    for match in re.finditer(r'(\w+)=("([^"]*)"|([^,]*))', parts[1]):
        key = match.group(1).lower()
        quoted = match.group(3)
        bare = match.group(4)
        params[key] = quoted if quoted is not None else bare.strip()
    if "realm" not in params:
        raise RegistryError("authentication challenge missing realm: %s" % header)
    return params


def append_query(url, values):
    parsed = urlparse.urlsplit(url)
    query = dict(urlparse.parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in values.items():
        if value:
            query[key] = value
    return urlparse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlparse.urlencode(query),
            parsed.fragment,
        )
    )


def response_header(headers, name):
    try:
        return headers.get(name)
    except AttributeError:
        return None


class RegistryClient(object):
    def __init__(self, registry, repository, opener, timeout=60, retries=3, chunk_size=8 * 1024 * 1024):
        self.registry = registry
        self.repository = repository
        self.opener = opener
        self.timeout = timeout
        self.retries = retries
        self.chunk_size = chunk_size
        self.token = None

    def manifest_url(self, reference):
        return "https://%s/v2/%s/manifests/%s" % (
            self.registry,
            self.repository,
            urlparse.quote(reference, safe=":"),
        )

    def blob_url(self, digest):
        return "https://%s/v2/%s/blobs/%s" % (
            self.registry,
            self.repository,
            urlparse.quote(digest, safe=":"),
        )

    def fetch_manifest(self, reference, accept_header):
        data, headers = self.request_bytes(
            self.manifest_url(reference), {"Accept": accept_header}
        )
        try:
            document = json.loads(data.decode("utf-8"))
        except ValueError as exc:
            raise RegistryError("manifest response is not valid JSON: %s" % exc)
        digest = response_header(headers, "Docker-Content-Digest")
        if not digest:
            digest = compute_sha256_digest_bytes(data)
        media_type = response_header(headers, "Content-Type") or document.get("mediaType") or ""
        media_type = media_type.split(";", 1)[0].strip()
        return document, data, digest, media_type

    def request_bytes(self, url, headers=None):
        response = self.open_with_auth(url, headers or {})
        with response:
            return response.read(), response.headers

    def download_blob(self, descriptor, dest_dir, cache_dir=None, progress=None):
        digest = descriptor.get("digest")
        if not digest:
            raise RegistryError("blob descriptor missing digest: %s" % descriptor)
        expected_size = descriptor.get("size")
        _, hex_digest = digest_parts(digest)
        if cache_dir is not None:
            dest_path = cache_blob_path(cache_dir, digest)
            tmp_path = cache_part_path(cache_dir, digest)
        else:
            dest_path = Path(dest_dir) / hex_digest
            tmp_path = Path(str(dest_path) + ".part")

        if dest_path.exists():
            verify_digest(dest_path, digest, expected_size)
            if progress is not None:
                progress.update(expected_size if expected_size is not None else dest_path.stat().st_size)
            return dest_path

        last_error = None
        for attempt in range(1, self.retries + 1):
            if attempt > 1 and progress is not None:
                progress.reset_current()
            try:
                return self.download_blob_once(descriptor, dest_path, tmp_path, progress)
            except RegistryError as exc:
                last_error = exc
                if is_validation_error(exc):
                    if tmp_path.exists():
                        tmp_path.unlink()
                    if dest_path.exists():
                        dest_path.unlink()
                if attempt == self.retries:
                    raise RegistryError(
                        "failed to download blob %s after %s attempts: %s"
                        % (digest, self.retries, exc)
                    )
                time.sleep(min(2 ** (attempt - 1), 8))

        raise RegistryError("failed to download blob %s: %s" % (digest, last_error))

    def download_blob_once(self, descriptor, dest_path, tmp_path, progress=None):
        digest = descriptor.get("digest")
        expected_size = descriptor.get("size")
        dest_path = Path(dest_path)
        tmp_path = Path(tmp_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.parent.mkdir(parents=True, exist_ok=True)

        if tmp_path.exists() and expected_size is not None:
            partial_size = tmp_path.stat().st_size
            if partial_size == expected_size:
                verify_digest(tmp_path, digest, expected_size)
                tmp_path.replace(dest_path)
                if progress is not None:
                    progress.update(expected_size)
                return dest_path
            if partial_size > expected_size:
                tmp_path.unlink()

        partial_size = tmp_path.stat().st_size if tmp_path.exists() else 0
        headers = {}
        if partial_size > 0:
            headers["Range"] = "bytes=%s-" % partial_size
        url = self.blob_url(digest)
        response = self.open_with_auth(url, headers)
        mode = "wb"
        if partial_size > 0 and response_code(response) == 206:
            mode = "ab"
            if progress is not None:
                progress.update(partial_size)
        elif partial_size > 0:
            tmp_path.unlink()
            partial_size = 0

        try:
            with response:
                with open(str(tmp_path), mode) as handle:
                    while True:
                        chunk = response.read(self.chunk_size)
                        if not chunk:
                            break
                        handle.write(chunk)
                        if progress is not None:
                            progress.update(len(chunk))
        except Exception:
            raise RegistryError("network error while downloading blob %s" % digest)

        verify_digest(tmp_path, digest, expected_size)
        tmp_path.replace(dest_path)
        return dest_path

    def open_with_auth(self, url, headers):
        try:
            return self.open_url(url, headers, include_token=True)
        except urlerror.HTTPError as exc:
            if exc.code != 401:
                raise self.http_error(url, exc)
            challenge = response_header(exc.headers, "WWW-Authenticate")
            self.token = self.fetch_token(challenge)
            try:
                return self.open_url(url, headers, include_token=True)
            except urlerror.HTTPError as second:
                raise self.http_error(url, second)
        except urlerror.URLError as exc:
            raise RegistryError("network error for %s: %s" % (url, exc))

    def open_url(self, url, headers, include_token):
        request_headers = {
            "User-Agent": "docker-blob-downloader/1.0",
        }
        request_headers.update(headers or {})
        if include_token and self.token:
            request_headers["Authorization"] = "Bearer " + self.token
        request = urlrequest.Request(url, headers=request_headers)
        last_error = None
        for attempt in range(1, self.retries + 1):
            try:
                return self.opener.open(request, timeout=self.timeout)
            except urlerror.HTTPError as exc:
                if exc.code == 401:
                    raise
                if exc.code < 500 or attempt == self.retries:
                    raise
                last_error = exc
            except (urlerror.URLError, ssl.SSLError, OSError) as exc:
                if attempt == self.retries:
                    raise RegistryError("network error for %s: %s" % (url, exc))
                last_error = exc
            time.sleep(min(2 ** (attempt - 1), 8))
        raise RegistryError("request failed for %s: %s" % (url, last_error))

    def fetch_token(self, challenge_header):
        params = parse_www_authenticate(challenge_header)
        scope = params.get("scope") or "repository:%s:pull" % self.repository
        token_url = append_query(
            params["realm"], {"service": params.get("service"), "scope": scope}
        )
        request = urlrequest.Request(
            token_url, headers={"User-Agent": "docker-blob-downloader/1.0"}
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
        except urlerror.HTTPError as exc:
            raise self.http_error(token_url, exc)
        except urlerror.URLError as exc:
            raise RegistryError("token request failed for %s: %s" % (token_url, exc))

        try:
            document = json.loads(payload.decode("utf-8"))
        except ValueError as exc:
            raise RegistryError("token response is not valid JSON: %s" % exc)
        token = document.get("token") or document.get("access_token")
        if not token:
            raise RegistryError("token response did not include token or access_token")
        return token

    def http_error(self, url, exc):
        body = b""
        try:
            body = exc.read(4096)
        except Exception:
            body = b""
        snippet = body.decode("utf-8", "replace").strip()
        if snippet:
            return RegistryError("HTTP %s for %s: %s" % (exc.code, url, snippet))
        return RegistryError("HTTP %s for %s" % (exc.code, url))


def build_opener(http_proxy=None, https_proxy=None, no_env_proxy=False, insecure=False):
    handlers = []
    if no_env_proxy:
        proxies = {}
    else:
        proxies = urlrequest.getproxies()
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    handlers.append(urlrequest.ProxyHandler(proxies))
    if insecure:
        context = ssl._create_unverified_context()
        handlers.append(urlrequest.HTTPSHandler(context=context))
    return urlrequest.build_opener(*handlers)


def is_index_manifest(document, media_type):
    return media_type in INDEX_MEDIA_TYPES or (
        "manifests" in document and "config" not in document
    )


def ensure_image_manifest(document, media_type):
    if media_type and media_type not in IMAGE_MANIFEST_MEDIA_TYPES:
        if "config" not in document or "layers" not in document:
            raise RegistryError("unsupported manifest media type: %s" % media_type)
    if "config" not in document or "layers" not in document:
        raise RegistryError("image manifest is missing config or layers")


def safe_filename(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def default_output_path(ref, os_name, arch):
    name = "%s_%s_%s_%s_%s.tar" % (
        ref.registry,
        ref.repository,
        ref.reference.replace(":", "_"),
        os_name,
        normalize_arch(arch),
    )
    return Path(safe_filename(name))


def resolve_image(client, ref, os_name, arch, variant):
    manifest, manifest_bytes, manifest_digest, media_type = client.fetch_manifest(
        ref.reference, MANIFEST_ACCEPT
    )
    selected_platform = None
    if is_index_manifest(manifest, media_type):
        selected = select_platform_manifest(manifest, os_name, arch, variant)
        selected_platform = selected.get("platform") or {}
        manifest, manifest_bytes, manifest_digest, media_type = client.fetch_manifest(
            selected["digest"], IMAGE_MANIFEST_ACCEPT
        )
    ensure_image_manifest(manifest, media_type)
    if not selected_platform:
        selected_platform = {
            "os": os_name,
            "architecture": normalize_arch(arch),
        }
    return manifest, manifest_bytes, manifest_digest, media_type, selected_platform


def descriptor_text(descriptor):
    return "%s %s bytes %s" % (
        descriptor.get("digest", "<missing-digest>"),
        descriptor.get("size", "unknown"),
        descriptor.get("mediaType", "<missing-media-type>"),
    )


def print_dry_run(ref, manifest, manifest_digest, media_type, selected_platform):
    print("image: %s" % ref.original)
    print("repo tag: %s" % (ref.repo_tag() or "<digest reference>"))
    print("platform: %s" % platform_string(selected_platform))
    print("manifest: %s %s" % (manifest_digest, media_type))
    print("config: %s" % descriptor_text(manifest["config"]))
    layers = manifest.get("layers") or []
    print("layers: %s" % len(layers))
    for index, layer in enumerate(layers, 1):
        print("  %s. %s" % (index, descriptor_text(layer)))


def download_image(args):
    ref = parse_image_reference(args.image)
    opener = build_opener(
        http_proxy=args.http_proxy,
        https_proxy=args.https_proxy,
        no_env_proxy=args.no_env_proxy,
        insecure=args.insecure,
    )
    client = RegistryClient(
        ref.registry,
        ref.repository,
        opener,
        timeout=args.timeout,
        retries=args.retries,
        chunk_size=args.chunk_size,
    )
    manifest, manifest_bytes, manifest_digest, media_type, platform = resolve_image(
        client, ref, args.os, args.arch, args.variant
    )
    if args.dry_run:
        print_dry_run(ref, manifest, manifest_digest, media_type, platform)
        return None

    output = Path(args.output) if args.output else default_output_path(ref, args.os, args.arch)
    temp_parent = output.parent if str(output.parent) else Path(".")
    if not temp_parent.exists():
        raise RegistryError("output directory does not exist: %s" % temp_parent)

    cache_dir = None
    if not args.no_cache:
        cache_dir = Path(args.cache_dir).expanduser() if args.cache_dir else default_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        print("using cache: %s" % cache_dir, file=sys.stderr)

    progress = ProgressReporter(stream=sys.stderr, enabled=not args.no_progress)

    with tempfile.TemporaryDirectory(prefix="download-", dir=str(temp_parent)) as tmp:
        tmp_path = Path(tmp)
        config_descriptor = manifest["config"]
        layers = manifest.get("layers") or []
        total_blobs = 1 + len(layers)

        def fetch_blob(index, label, descriptor):
            if progress.enabled:
                progress.start_blob(
                    index,
                    total_blobs,
                    descriptor.get("digest", ""),
                    descriptor.get("size"),
                )
            else:
                print("fetching %s %s" % (label, descriptor_text(descriptor)), file=sys.stderr)
            path = client.download_blob(
                descriptor,
                tmp_path,
                cache_dir=cache_dir,
                progress=progress if progress.enabled else None,
            )
            if progress.enabled:
                progress.finish_blob()
            return path

        config_path = fetch_blob(1, "config", config_descriptor)
        layer_paths = []
        for index, layer in enumerate(layers, 1):
            layer_paths.append(fetch_blob(index + 1, "layer %s/%s" % (index, len(layers)), layer))
        annotations = {}
        if not ref.is_digest:
            annotations["org.opencontainers.image.ref.name"] = ref.reference
        write_oci_archive(
            output_path=output,
            repo_tag=ref.repo_tag(),
            manifest_bytes=manifest_bytes,
            manifest_digest=manifest_digest,
            manifest_media_type=media_type,
            config_path=config_path,
            layer_paths=layer_paths,
            annotations=annotations,
        )
    print("wrote archive: %s" % output, file=sys.stderr)
    return output


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Download Docker image blobs and assemble an OCI archive."
    )
    parser.add_argument("image", nargs="?", default=DEFAULT_IMAGE, help="image reference")
    parser.add_argument(
        "--arch",
        default=DEFAULT_ARCH,
        help="target architecture, default: %(default)s",
    )
    parser.add_argument("--os", default=DEFAULT_OS, help="target OS, default: %(default)s")
    parser.add_argument("--variant", default=None, help="target platform variant")
    parser.add_argument("-o", "--output", default=None, help="output archive tar path")
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="persistent blob cache directory, default: ~/.cache/docker-blob-downloader",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="disable persistent blob cache and use only a temporary directory",
    )
    parser.add_argument("--http-proxy", default=None, help="HTTP proxy URL")
    parser.add_argument("--https-proxy", default=None, help="HTTPS proxy URL")
    parser.add_argument(
        "--no-env-proxy",
        action="store_true",
        help="ignore proxy settings from environment variables",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="disable TLS certificate verification",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve the manifest and list blobs without downloading them",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="disable stderr progress output",
    )
    parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout in seconds")
    parser.add_argument("--retries", type=int, default=3, help="HTTP retry count")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=8 * 1024 * 1024,
        help="download chunk size in bytes",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print traceback for failures",
    )
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    if args.retries <= 0:
        parser.error("--retries must be greater than 0")
    if args.chunk_size <= 0:
        parser.error("--chunk-size must be greater than 0")
    return args


def main(argv=None):
    args = None
    try:
        args = parse_args(argv)
        if args.insecure:
            print(
                "WARNING: TLS certificate verification disabled by --insecure",
                file=sys.stderr,
            )
        download_image(args)
        return 0
    except KeyboardInterrupt:
        print("ERROR: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        if args is not None and args.verbose:
            traceback.print_exc()
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

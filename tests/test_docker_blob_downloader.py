import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import docker_blob_downloader as dbd


def sha256_digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FakeResponse:
    def __init__(self, data, code=200):
        self._data = data
        self._offset = 0
        self._code = code
        self.headers = {}

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._data) - self._offset
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def getcode(self):
        return self._code

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeOpener:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("no fake responses left")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ImageReferenceTests(unittest.TestCase):
    def test_default_reference_uses_requested_quay_image(self):
        ref = dbd.parse_image_reference(dbd.DEFAULT_IMAGE)

        self.assertEqual(ref.registry, "quay.io")
        self.assertEqual(ref.repository, "ascend/vllm-ascend")
        self.assertEqual(ref.reference, "deepseekv4-a3")
        self.assertEqual(ref.original, dbd.DEFAULT_IMAGE)

    def test_docker_hub_short_name_defaults_to_library_latest(self):
        ref = dbd.parse_image_reference("busybox")

        self.assertEqual(ref.registry, "registry-1.docker.io")
        self.assertEqual(ref.repository, "library/busybox")
        self.assertEqual(ref.reference, "latest")


class PlatformTests(unittest.TestCase):
    def test_aarch64_normalizes_to_registry_arm64(self):
        self.assertEqual(dbd.normalize_arch("aarch64"), "arm64")

    def test_select_platform_manifest_matches_normalized_architecture(self):
        index = {
            "manifests": [
                {
                    "digest": "sha256:amd64",
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {
                    "digest": "sha256:arm64",
                    "platform": {"os": "linux", "architecture": "arm64"},
                },
            ]
        }

        selected = dbd.select_platform_manifest(index, "linux", "aarch64", None)

        self.assertEqual(selected["digest"], "sha256:arm64")

    def test_select_platform_manifest_reports_available_platforms(self):
        index = {
            "manifests": [
                {
                    "digest": "sha256:amd64",
                    "platform": {"os": "linux", "architecture": "amd64"},
                }
            ]
        }

        with self.assertRaisesRegex(dbd.RegistryError, "available platforms: linux/amd64"):
            dbd.select_platform_manifest(index, "linux", "aarch64", None)


class BlobVerificationTests(unittest.TestCase):
    def test_verify_digest_accepts_matching_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blob"
            data = b"registry blob data"
            path.write_bytes(data)
            digest = "sha256:" + hashlib.sha256(data).hexdigest()

            dbd.verify_digest(path, digest, len(data))

    def test_verify_digest_rejects_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blob"
            path.write_bytes(b"wrong data")

            with self.assertRaisesRegex(dbd.RegistryError, "digest mismatch"):
                dbd.verify_digest(path, "sha256:" + ("0" * 64), None)

    def test_verify_digest_rejects_size_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blob"
            path.write_bytes(b"1234")
            digest = "sha256:" + hashlib.sha256(b"1234").hexdigest()

            with self.assertRaisesRegex(dbd.RegistryError, "size mismatch"):
                dbd.verify_digest(path, digest, 3)


class CachePathTests(unittest.TestCase):
    def test_cache_blob_path_is_digest_addressed(self):
        digest = "sha256:" + ("a" * 64)
        path = dbd.cache_blob_path(Path("/tmp/cache"), digest)

        self.assertEqual(path, Path("/tmp/cache/blobs/sha256/" + ("a" * 64)))

    def test_cache_part_path_keeps_partial_file_next_to_final_blob(self):
        digest = "sha256:" + ("b" * 64)
        path = dbd.cache_part_path(Path("/tmp/cache"), digest)

        self.assertEqual(path, Path("/tmp/cache/blobs/sha256/" + ("b" * 64) + ".part"))


class DownloadBlobTests(unittest.TestCase):
    def test_download_blob_retries_after_digest_mismatch(self):
        good_data = b"complete blob"
        digest = sha256_digest(good_data)
        opener = FakeOpener([FakeResponse(b"bad blob"), FakeResponse(good_data)])
        client = dbd.RegistryClient("example.test", "repo/image", opener, retries=2)

        with tempfile.TemporaryDirectory() as tmp:
            path = client.download_blob(
                {"digest": digest, "size": len(good_data)},
                Path(tmp) / "work",
                cache_dir=Path(tmp) / "cache",
            )

            self.assertEqual(path.read_bytes(), good_data)
            self.assertEqual(len(opener.requests), 2)

    def test_download_blob_resumes_partial_file_with_range_header(self):
        full_data = b"partial-then-rest"
        partial = full_data[:7]
        rest = full_data[7:]
        digest = sha256_digest(full_data)
        opener = FakeOpener([FakeResponse(rest, code=206)])
        client = dbd.RegistryClient("example.test", "repo/image", opener, retries=1)

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            part_path = dbd.cache_part_path(cache_dir, digest)
            part_path.parent.mkdir(parents=True)
            part_path.write_bytes(partial)

            path = client.download_blob(
                {"digest": digest, "size": len(full_data)},
                Path(tmp) / "work",
                cache_dir=cache_dir,
            )

            self.assertEqual(path.read_bytes(), full_data)
            self.assertEqual(opener.requests[0].get_header("Range"), "bytes=7-")


class ProgressReporterTests(unittest.TestCase):
    def test_progress_reporter_writes_percentage_and_bytes(self):
        stream = io.StringIO()
        reporter = dbd.ProgressReporter(stream=stream, enabled=True)

        reporter.start_blob(1, 2, "sha256:" + ("c" * 64), 10)
        reporter.update(5)
        reporter.finish_blob()

        output = stream.getvalue()
        self.assertIn("50.0%", output)
        self.assertIn("5/10 B", output)
        self.assertIn("1/2", output)


class ArchiveTests(unittest.TestCase):
    def test_write_oci_archive_contains_index_layout_manifest_and_blobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            layer = root / "layer"
            config.write_bytes(json.dumps({"architecture": "arm64", "os": "linux"}).encode())
            layer.write_bytes(b"layer")
            config_digest = "sha256:" + hashlib.sha256(config.read_bytes()).hexdigest()
            layer_digest = "sha256:" + hashlib.sha256(layer.read_bytes()).hexdigest()
            manifest = {
                "schemaVersion": 2,
                "mediaType": dbd.OCI_MANIFEST_MEDIA_TYPE,
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": config_digest,
                    "size": config.stat().st_size,
                },
                "layers": [
                    {
                        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                        "digest": layer_digest,
                        "size": layer.stat().st_size,
                    }
                ],
            }
            manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
            manifest_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
            output = root / "image.tar"

            dbd.write_oci_archive(
                output_path=output,
                repo_tag="quay.io/ascend/vllm-ascend:deepseekv4-a3",
                manifest_bytes=manifest_bytes,
                manifest_digest=manifest_digest,
                manifest_media_type=dbd.OCI_MANIFEST_MEDIA_TYPE,
                config_path=config,
                layer_paths=[layer],
                annotations={"org.opencontainers.image.ref.name": "deepseekv4-a3"},
            )

            import tarfile

            with tarfile.open(output, "r") as tar:
                names = set(tar.getnames())

            self.assertIn("oci-layout", names)
            self.assertIn("index.json", names)
            self.assertIn("manifest.json", names)
            self.assertIn("blobs/sha256/" + manifest_digest.split(":", 1)[1], names)
            self.assertIn("blobs/sha256/" + config_digest.split(":", 1)[1], names)
            self.assertIn("blobs/sha256/" + layer_digest.split(":", 1)[1], names)


if __name__ == "__main__":
    unittest.main()

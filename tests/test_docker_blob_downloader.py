import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import docker_blob_downloader as dbd


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

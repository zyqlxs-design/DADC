from __future__ import annotations

import hashlib
import http.client
import importlib.util
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _load_downloader():
    path = REPOSITORY_ROOT / "scripts" / "download_we_inductor_sample.py"
    spec = importlib.util.spec_from_file_location("dadc_inductor_downloader", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load downloader: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, data: bytes, content_type: str):
        self._data = data
        self._offset = 0
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, amount: int) -> bytes:
        result = self._data[self._offset : self._offset + amount]
        self._offset += len(result)
        return result


class InductorDownloaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.downloader = _load_downloader()
        self._temporary = tempfile.TemporaryDirectory(prefix="dadc-downloader-")
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_incomplete_chunked_response_is_retried_without_leaving_part_file(self) -> None:
        target = self.root / "sample.s2p"
        data = b"! source\n# Hz S RI R 50\n1 0 0\n"
        expected = hashlib.sha256(data).hexdigest()
        self.downloader.EXPECTED_SHA256[target.name] = expected
        responses = [http.client.IncompleteRead(b"partial"), _Response(data, "text/plain")]

        def open_response(*_args, **_kwargs):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(self.downloader.urllib.request, "urlopen", side_effect=open_response):
            result = self.downloader._download(
                "https://example.invalid/sample.s2p",
                target,
                max_attempts=2,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(2, result["download_attempts"])
        self.assertEqual(data, target.read_bytes())
        self.assertFalse(target.with_suffix(".s2p.part").exists())

    def test_wrong_pdf_hash_is_retried_and_only_pinned_bytes_are_published(self) -> None:
        target = self.root / "sample.pdf"
        accepted = b"%PDF-1.7\naccepted\n"
        rejected = b"%PDF-1.7\nunreviewed\n"
        self.downloader.EXPECTED_SHA256[target.name] = hashlib.sha256(accepted).hexdigest()
        responses = [
            _Response(rejected, "application/pdf"),
            _Response(accepted, "application/pdf"),
        ]
        with patch.object(
            self.downloader.urllib.request,
            "urlopen",
            side_effect=lambda *_args, **_kwargs: responses.pop(0),
        ):
            result = self.downloader._download(
                "https://example.invalid/sample.pdf",
                target,
                max_attempts=2,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(2, result["download_attempts"])
        self.assertEqual(accepted, target.read_bytes())

    def test_non_pdf_response_is_never_published(self) -> None:
        target = self.root / "sample.pdf"
        html = b"<html>temporary CDN response</html>"
        self.downloader.EXPECTED_SHA256[target.name] = hashlib.sha256(html).hexdigest()
        with patch.object(
            self.downloader.urllib.request,
            "urlopen",
            return_value=_Response(html, "text/html"),
        ):
            with self.assertRaisesRegex(RuntimeError, "expected application/pdf"):
                self.downloader._download(
                    "https://example.invalid/sample.pdf",
                    target,
                    max_attempts=1,
                    sleep=lambda _seconds: None,
                )
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

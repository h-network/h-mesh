import base64
import sys
import unittest
from pathlib import Path

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.channels import DeadLetter
from lib.attachment_schema import (
    ATTACHMENT_MAX_BYTES,
    ATTACHMENT_MAX_BASE64_CHARS,
    ValidatedAttachment,
    validate_attachment,
    validate_attachment_payload,
)


class AttachmentSchemaTests(unittest.TestCase):
    def test_valid_attachment_payload(self):
        content = b"Hello, World!"
        b64 = base64.b64encode(content).decode("ascii")
        payload = {
            "filename": "hello.txt",
            "mime_type": "text/plain",
            "content_base64": b64,
            "caption": "A greeting file",
        }
        res = validate_attachment_payload(payload)
        self.assertIsInstance(res, ValidatedAttachment)
        self.assertEqual(res.filename, "hello.txt")
        self.assertEqual(res.mime_type, "text/plain")
        self.assertEqual(res.data, content)
        self.assertEqual(res.caption, "A greeting file")

        # Check tuple unpacking
        fn, mt, data, cap = res
        self.assertEqual((fn, mt, data, cap), ("hello.txt", "text/plain", content, "A greeting file"))

    def test_validate_attachment_with_envelope(self):
        content = b"test content"
        b64 = base64.b64encode(content).decode("ascii")
        envelope = {
            "kind": "Attachment",
            "stream_id": "stream123",
            "payload": {
                "filename": "test.bin",
                "mime_type": "application/octet-stream",
                "content_base64": b64,
            },
        }
        res = validate_attachment(envelope)
        self.assertEqual(res.filename, "test.bin")
        self.assertEqual(res.data, content)
        self.assertIsNone(res.caption)

    def test_payload_not_dict(self):
        with self.assertRaises(DeadLetter) as cm:
            validate_attachment_payload("not-a-dict")
        self.assertIn("must be a dict", str(cm.exception))

    def test_missing_required_keys(self):
        with self.assertRaises(DeadLetter) as cm:
            validate_attachment_payload({"filename": "a.txt", "mime_type": "text/plain"})
        self.assertIn("missing required attachment payload fields", str(cm.exception))

    def test_unexpected_keys(self):
        b64 = base64.b64encode(b"abc").decode("ascii")
        payload = {
            "filename": "a.txt",
            "mime_type": "text/plain",
            "content_base64": b64,
            "extra_key": 123,
        }
        with self.assertRaises(DeadLetter) as cm:
            validate_attachment_payload(payload)
        self.assertIn("unexpected attachment payload fields", str(cm.exception))

    def test_invalid_field_types(self):
        b64 = base64.b64encode(b"abc").decode("ascii")
        with self.assertRaises(DeadLetter) as cm:
            validate_attachment_payload({"filename": 123, "mime_type": "text/plain", "content_base64": b64})
        self.assertIn("invalid attachment payload field types", str(cm.exception))

        with self.assertRaises(DeadLetter) as cm:
            validate_attachment_payload({"filename": "a.txt", "mime_type": "text/plain", "content_base64": b64, "caption": 123})
        self.assertIn("caption must be a string", str(cm.exception))

    def test_filename_validation(self):
        b64 = base64.b64encode(b"abc").decode("ascii")
        base = {"mime_type": "text/plain", "content_base64": b64}

        for bad_name, err in [
            ("", "filename length must be between 1 and 255"),
            ("a" * 256, "filename length must be between 1 and 255"),
            (".", "filename cannot be '.' or '..'"),
            ("..", "filename cannot be '.' or '..'"),
            ("foo/bar", "filename cannot contain path separators"),
            ("foo\\bar", "filename cannot contain path separators"),
            ("foo\x00bar", "filename cannot contain ASCII control characters"),
            ("foo\nbar", "filename cannot contain ASCII control characters"),
            ("foo\x1fbar", "filename cannot contain ASCII control characters"),
            ("foo\x7fbar", "filename cannot contain ASCII control characters"),
        ]:
            with self.subTest(bad_name=bad_name):
                with self.assertRaises(DeadLetter) as cm:
                    validate_attachment_payload({**base, "filename": bad_name})
                self.assertIn(err, str(cm.exception))

    def test_mime_type_validation(self):
        b64 = base64.b64encode(b"abc").decode("ascii")
        base = {"filename": "a.txt", "content_base64": b64}

        for bad_mime in ["", "text", "text/", "/plain", "text/plain/extra", "text/pl@in", "t ext/plain", "é/plain"]:
            with self.subTest(bad_mime=bad_mime):
                with self.assertRaises(DeadLetter):
                    validate_attachment_payload({**base, "mime_type": bad_mime})

    def test_caption_length_validation(self):
        b64 = base64.b64encode(b"abc").decode("ascii")
        payload = {
            "filename": "a.txt",
            "mime_type": "text/plain",
            "content_base64": b64,
            "caption": "x" * 65537,
        }
        with self.assertRaises(DeadLetter) as cm:
            validate_attachment_payload(payload)
        self.assertIn("caption exceeds 65536 UTF-8 bytes", str(cm.exception))

    def test_base64_validation(self):
        base = {"filename": "a.txt", "mime_type": "text/plain"}

        # Length not multiple of 4
        with self.assertRaises(DeadLetter) as cm:
            validate_attachment_payload({**base, "content_base64": "abc"})
        self.assertIn("multiple of 4", str(cm.exception))

        # Invalid base64 characters / whitespace / URL safe
        with self.assertRaises(DeadLetter) as cm:
            validate_attachment_payload({**base, "content_base64": "abc-def="})
        self.assertIn("invalid characters or malformed padding", str(cm.exception))

        # Exceeds max base64 chars
        with self.assertRaises(DeadLetter) as cm:
            validate_attachment_payload({**base, "content_base64": "A" * (ATTACHMENT_MAX_BASE64_CHARS + 4)})
        self.assertIn("exceeds maximum allowed base64 length", str(cm.exception))

    def test_max_bytes_validation(self):
        # 10 MiB + 1 byte
        oversized = b"a" * (ATTACHMENT_MAX_BYTES + 1)
        b64 = base64.b64encode(oversized).decode("ascii")
        payload = {
            "filename": "big.bin",
            "mime_type": "application/octet-stream",
            "content_base64": b64,
        }
        with self.assertRaises(DeadLetter) as cm:
            validate_attachment_payload(payload)
        self.assertIn("exceeds maximum size", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

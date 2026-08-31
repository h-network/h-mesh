"""Attachment wire/schema limits shared by every port that delivers attachments."""

import base64
import math
import re
from typing import NamedTuple

from core.channels import DeadLetter

ATTACHMENT_MAX_BYTES = 10_485_760  # 10 MiB
ATTACHMENT_MAX_BASE64_CHARS = 4 * math.ceil(ATTACHMENT_MAX_BYTES / 3)  # 13_981_016
MIME_TYPE_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$")
BASE64_CHARS_REGEX = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")


class ValidatedAttachment(NamedTuple):
    filename: str
    mime_type: str
    data: bytes
    caption: str | None


def validate_attachment_payload(payload: dict) -> ValidatedAttachment:
    """Validate an attachment payload against schema limits and return decoded contents.

    Raises DeadLetter on any schema, encoding, or size violation.
    """
    if not isinstance(payload, dict):
        raise DeadLetter("attachment payload must be a dict")

    required_keys = {"filename", "mime_type", "content_base64"}
    allowed_keys = {"filename", "mime_type", "content_base64", "caption"}

    if not required_keys.issubset(payload.keys()):
        raise DeadLetter("missing required attachment payload fields")
    if not set(payload.keys()).issubset(allowed_keys):
        raise DeadLetter("unexpected attachment payload fields")

    filename = payload["filename"]
    mime_type = payload["mime_type"]
    content_base64 = payload["content_base64"]
    caption = payload.get("caption")

    if not isinstance(filename, str) or not isinstance(mime_type, str) or not isinstance(content_base64, str):
        raise DeadLetter("invalid attachment payload field types")
    if caption is not None and not isinstance(caption, str):
        raise DeadLetter("caption must be a string if present")

    # Validate filename: non-empty UTF-8 basename of at most 255 UTF-8 bytes.
    # May not be '.' or '..', contain '/', '\\', NUL, ASCII controls (< 32), or U+007F.
    try:
        filename_bytes = filename.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DeadLetter(f"filename utf-8 encoding error: {exc}") from exc
    if not (1 <= len(filename_bytes) <= 255):
        raise DeadLetter("filename length must be between 1 and 255 UTF-8 bytes")
    if filename in {".", ".."}:
        raise DeadLetter("filename cannot be '.' or '..'")
    if "/" in filename or "\\" in filename:
        raise DeadLetter("filename cannot contain path separators")
    if any(ord(c) < 32 or ord(c) == 127 for c in filename):
        raise DeadLetter("filename cannot contain ASCII control characters or DEL")

    # Validate mime_type: at most 255 ASCII bytes and matches ^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$
    try:
        mime_bytes = mime_type.encode("ascii")
    except UnicodeEncodeError as exc:
        raise DeadLetter(f"mime_type must be ASCII: {exc}") from exc
    if not (1 <= len(mime_bytes) <= 255):
        raise DeadLetter("mime_type length must be between 1 and 255 ASCII bytes")
    if not MIME_TYPE_REGEX.match(mime_type):
        raise DeadLetter(f"invalid mime_type format: {mime_type!r}")

    # Validate caption: at most 65,536 UTF-8 bytes
    if caption is not None:
        try:
            caption_bytes = caption.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise DeadLetter(f"caption utf-8 encoding error: {exc}") from exc
        if len(caption_bytes) > 65536:
            raise DeadLetter("caption exceeds 65536 UTF-8 bytes")

    # Validate content_base64:
    # RFC 4648 standard base64 with padding, decoded with strict alphabet validation.
    # URL-safe base64 and whitespace are rejected.
    if len(content_base64) > ATTACHMENT_MAX_BASE64_CHARS:
        raise DeadLetter("content_base64 exceeds maximum allowed base64 length")
    if len(content_base64) % 4 != 0:
        raise DeadLetter("content_base64 length must be a multiple of 4")
    if not BASE64_CHARS_REGEX.match(content_base64):
        raise DeadLetter("content_base64 contains invalid characters or malformed padding")
    try:
        decoded_bytes = base64.b64decode(content_base64, validate=True)
    except Exception as exc:
        raise DeadLetter(f"content_base64 decode failed: {exc}") from exc

    if len(decoded_bytes) > ATTACHMENT_MAX_BYTES:
        raise DeadLetter(f"decoded attachment exceeds maximum size of {ATTACHMENT_MAX_BYTES} bytes")

    return ValidatedAttachment(
        filename=filename,
        mime_type=mime_type,
        data=decoded_bytes,
        caption=caption,
    )


def validate_attachment(payload_or_envelope: dict) -> ValidatedAttachment:
    """Validate an attachment payload or envelope and return decoded contents."""
    if (
        isinstance(payload_or_envelope, dict)
        and "payload" in payload_or_envelope
        and isinstance(payload_or_envelope["payload"], dict)
    ):
        return validate_attachment_payload(payload_or_envelope["payload"])
    return validate_attachment_payload(payload_or_envelope)


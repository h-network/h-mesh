"""Attachment wire/schema limits shared by every port that delivers attachments."""

import math
import re

ATTACHMENT_MAX_BYTES = 10_485_760  # 10 MiB
ATTACHMENT_MAX_BASE64_CHARS = 4 * math.ceil(ATTACHMENT_MAX_BYTES / 3)  # 13_981_016
MIME_TYPE_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$")
BASE64_CHARS_REGEX = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")

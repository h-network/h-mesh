"""tmux control mode does not carry raw bytes — it octal-escapes them."""

import sys
import unittest
from pathlib import Path

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from modules.session.control import _unescape_control

ESC = b"\x1b"


class SessionControlEscapesTests(unittest.TestCase):
    def test_octal_escapes_become_the_bytes_they_stand_for(self):
        # ⚠ An operator saw screenfuls of `\033[?25l` rendered as prose because
        # %output was published unchanged. This is that bug.
        self.assertEqual(_unescape_control(b"\\033[?25l"), ESC + b"[?25l")
        self.assertEqual(
            _unescape_control(b"\\033[31;1mred\\033[0m"),
            ESC + b"[31;1mred" + ESC + b"[0m",
        )
        self.assertEqual(_unescape_control(b"\\015\\012"), b"\r\n")

    def test_a_literal_backslash_arrives_doubled_and_leaves_single(self):
        self.assertEqual(_unescape_control(b"a\\\\b"), b"a\\b")

    def test_ordinary_text_is_untouched(self):
        self.assertEqual(_unescape_control(b"hello world"), b"hello world")

    def test_non_ascii_output_decoding(self):
        # UTF-8 encoded text for "hello ’ world" (contains right single quotation mark U+2019)
        utf8_bytes = "hello ’ world".encode("utf-8")
        unescaped = _unescape_control(utf8_bytes)
        decoded = unescaped.decode("utf-8", errors="replace")
        self.assertEqual(decoded, "hello ’ world")
        self.assertEqual(len(decoded), 13)


if __name__ == "__main__":
    unittest.main()

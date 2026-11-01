import json
import sys
import unittest
from pathlib import Path

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from lib.coop_tokens import coop_tokens_key, grant, list_tokens, revoke, verify
from tests.test_api import FakeRedis


class CoopTokensTests(unittest.TestCase):
    def setUp(self):
        self.r = FakeRedis()

    def test_grant_shape_and_only_digest_is_stored(self):
        token_id, token = grant(self.r, "p", "t", "coop-a")
        prefix_, tid, secret = token.split(".")
        self.assertEqual((prefix_, tid), ("coop", token_id))
        self.assertRegex(token_id, r"^[0-9a-f]{16}$")
        self.assertEqual(len(secret), 43)
        stored = self.r.hashes[coop_tokens_key("p", "t")][token_id]
        self.assertNotIn(secret, stored)
        record = json.loads(stored)
        self.assertEqual(record["participant"], "coop-a")
        self.assertEqual(record["kinds"], ["Message"])
        self.assertIs(record["revoked"], False)
        self.assertEqual(len(record["sha256"]), 64)

    def test_verify_resolves_principal(self):
        token_id, token = grant(self.r, "p", "t", "coop-a")
        self.assertEqual(
            verify(self.r, "p", "t", token),
            {"token_id": token_id, "participant": "coop-a", "kinds": ["Message"]},
        )

    def test_verify_rejects_wrong_secret_other_office_and_garbage(self):
        token_id, token = grant(self.r, "p", "t", "coop-a")
        self.assertIsNone(verify(self.r, "p", "t", f"coop.{token_id}.{'A' * 43}"))
        self.assertIsNone(verify(self.r, "p", "other", token))
        for bad in ("", "coop", "x.y.z", None, 5, f"{token}.x"):
            self.assertIsNone(verify(self.r, "p", "t", bad))

    def test_revoke_is_immediate_and_idempotent(self):
        token_id, token = grant(self.r, "p", "t", "coop-a")
        self.assertTrue(revoke(self.r, "p", "t", token_id))
        self.assertIsNone(verify(self.r, "p", "t", token))
        self.assertFalse(revoke(self.r, "p", "t", token_id))
        self.assertFalse(revoke(self.r, "p", "t", "f" * 16))
        self.assertFalse(revoke(self.r, "p", "t", "not-an-id"))

    def test_list_has_no_secrets(self):
        a, token_a = grant(self.r, "p", "t", "coop-a")
        b, _ = grant(self.r, "p", "t", "coop-b")
        revoke(self.r, "p", "t", b)
        rows = list_tokens(self.r, "p", "t")
        self.assertEqual({row["token_id"]: row["revoked"] for row in rows}, {a: False, b: True})
        dumped = json.dumps(rows)
        self.assertNotIn(token_a.rsplit(".", 1)[1], dumped)
        self.assertNotIn("sha256", dumped)
        self.assertEqual(list_tokens(self.r, "p", "empty"), [])

    def test_grant_validates_participant_and_kinds(self):
        for bad in ("api", "host", "all", "Bad Name", "", "123"):
            with self.assertRaises(ValueError):
                grant(self.r, "p", "t", bad)
        for kinds in (("Command",), ("Message", "StartAgent"), ()):
            with self.assertRaises(ValueError):
                grant(self.r, "p", "t", "coop-a", kinds=kinds)

    def test_corrupt_record_fails_closed(self):
        token_id, token = grant(self.r, "p", "t", "coop-a")
        self.r.hashes[coop_tokens_key("p", "t")][token_id] = "not json"
        self.assertIsNone(verify(self.r, "p", "t", token))


if __name__ == "__main__":
    unittest.main()

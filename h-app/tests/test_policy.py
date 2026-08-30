import json
import sys
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch


H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.channels import send
from core.envelope import EnvelopeError
from core.keys import prefix
from core.policy import allows, require_allowed, tags_key


POD = "mesh"
TENANT = "office"
SOURCE = "alice"
DESTINATION = "bob"


class PolicyRedis:
    def __init__(self):
        self.hashes = defaultdict(dict)
        self.lists = defaultdict(list)

    def hget(self, key, field):
        return self.hashes[key].get(field)

    def hset(self, key, field, value):
        self.hashes[key][field] = value

    def hdel(self, key, field):
        return int(self.hashes[key].pop(field, None) is not None)

    def rpush(self, key, value):
        self.lists[key].append(value)
        return len(self.lists[key])


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.redis = PolicyRedis()
        registry = prefix(POD, TENANT, resource="registry")
        self.redis.hset(registry, SOURCE, "tmux")
        self.redis.hset(registry, DESTINATION, "tmux")

    def set_tags(self, agent, side, values):
        self.redis.hset(tags_key(POD, TENANT, agent), side, json.dumps(values))

    def test_matching_export_and_import_allow_send(self):
        self.set_tags(SOURCE, "export", ["work", "alerts"])
        self.set_tags(DESTINATION, "import", ["work"])
        self.assertTrue(
            allows(
                self.redis, pod=POD, tenant=TENANT,
                source=SOURCE, destination=DESTINATION,
            )
        )
        with patch("core.channels._emit_observation"):
            stream_id = send(
                self.redis, pod=POD, tenant=TENANT, source=SOURCE,
                destination=DESTINATION, payload={"text": "allowed"},
            )
        self.assertEqual(len(stream_id), 32)
        self.assertEqual(
            len(self.redis.lists[prefix(POD, TENANT, SOURCE, "egress")]), 1
        )

    def test_disjoint_export_and_import_deny_without_enqueue(self):
        self.set_tags(SOURCE, "export", ["alerts"])
        self.set_tags(DESTINATION, "import", ["work"])
        self.assertFalse(
            allows(
                self.redis, pod=POD, tenant=TENANT,
                source=SOURCE, destination=DESTINATION,
            )
        )
        with patch("core.channels.log_record") as log:
            with self.assertRaisesRegex(EnvelopeError, "no shared export/import tag"):
                send(
                    self.redis, pod=POD, tenant=TENANT, source=SOURCE,
                    destination=DESTINATION, payload={"text": "denied"},
                )
        self.assertEqual(self.redis.lists[prefix(POD, TENANT, SOURCE, "egress")], [])
        self.assertEqual(log.call_args.args[1], "send_refused")

    def test_missing_export_side_is_permissive(self):
        self.set_tags(DESTINATION, "import", ["work"])
        self.assertTrue(
            allows(
                self.redis, pod=POD, tenant=TENANT,
                source=SOURCE, destination=DESTINATION,
            )
        )
        self.assertIsNone(
            require_allowed(
                self.redis, pod=POD, tenant=TENANT,
                source=SOURCE, destination=DESTINATION,
            )
        )

    def test_missing_import_side_is_permissive(self):
        self.set_tags(SOURCE, "export", ["work"])
        self.assertTrue(
            allows(
                self.redis, pod=POD, tenant=TENANT,
                source=SOURCE, destination=DESTINATION,
            )
        )
        self.assertIsNone(
            require_allowed(
                self.redis, pod=POD, tenant=TENANT,
                source=SOURCE, destination=DESTINATION,
            )
        )

    def test_invalid_export_data_is_rejected(self):
        self.redis.hset(tags_key(POD, TENANT, SOURCE), "export", b'{"not":"a list"}')
        with self.assertRaisesRegex(EnvelopeError, "invalid export tags for 'alice'"):
            allows(
                self.redis, pod=POD, tenant=TENANT,
                source=SOURCE, destination=DESTINATION,
            )

    def test_invalid_import_data_is_rejected(self):
        self.set_tags(SOURCE, "export", ["work"])
        self.redis.hset(tags_key(POD, TENANT, DESTINATION), "import", "not-json")
        with self.assertRaisesRegex(EnvelopeError, "invalid import tags for 'bob'"):
            require_allowed(
                self.redis, pod=POD, tenant=TENANT,
                source=SOURCE, destination=DESTINATION,
            )


if __name__ == "__main__":
    unittest.main()

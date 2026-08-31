import sys
import threading
import unittest
from collections import defaultdict, deque
from pathlib import Path


H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.dispatch import (
    dispatch_ingress,
    get_handler,
    register_type,
    reset_registry,
    run_delivery_kick,
    unregister_type,
)
from core.keys import prefix


POD = "mesh"
TENANT = "office"
AGENT = "worker"


class DispatchRedis:
    def __init__(self):
        self.hashes = defaultdict(dict)
        self.lists = defaultdict(deque)
        self.values = {}
        self.lock = threading.Lock()
        self.hsetnx_contention = threading.Event()
        self.after_drain = None
        self.eval_calls = 0

    def get(self, key):
        with self.lock:
            return self.values.get(key)

    def set(self, key, value):
        with self.lock:
            self.values[key] = value

    def hget(self, key, field):
        with self.lock:
            return self.hashes[key].get(field)

    def hset(self, key, field, value):
        with self.lock:
            self.hashes[key][field] = value

    def hsetnx(self, key, field, value):
        with self.lock:
            if field in self.hashes[key]:
                self.hsetnx_contention.set()
                return 0
            self.hashes[key][field] = value
            return 1

    def hdel(self, key, field):
        with self.lock:
            return int(self.hashes[key].pop(field, None) is not None)

    def lrange(self, key, start, stop):
        with self.lock:
            values = list(self.lists[key])
        return values[start:] if stop == -1 else values[start : stop + 1]

    def delete(self, key):
        with self.lock:
            existed = key in self.lists or key in self.values or key in self.hashes
            self.lists.pop(key, None)
            self.values.pop(key, None)
            self.hashes.pop(key, None)
            return int(existed)

    def rpush(self, key, *values):
        with self.lock:
            self.lists[key].extend(values)
            return len(self.lists[key])

    def eval(self, script, key_count, *args):
        self.assert_drain_script(script, key_count)
        key = args[0]
        with self.lock:
            self.eval_calls += 1
            items = list(self.lists.pop(key, ()))
        if self.after_drain is not None:
            self.after_drain()
        return items

    @staticmethod
    def assert_drain_script(script, key_count):
        if "core unroutable ingress drain" not in script or key_count != 1:
            raise AssertionError("unexpected Redis script")


class DispatchTests(unittest.TestCase):
    def setUp(self):
        reset_registry()
        self.addCleanup(reset_registry)
        self.redis = DispatchRedis()
        self.registry = prefix(POD, TENANT, resource="registry")

    def test_dispatch_calls_registered_handler_with_destination_context(self):
        calls = []

        def handler(**kwargs):
            calls.append(kwargs)

        register_type("example", handler)
        self.redis.hset(self.registry, AGENT, b"example")
        dispatch_ingress(self.redis, pod=POD, tenant=TENANT, agent=AGENT)
        self.assertEqual(
            calls,
            [{"r": self.redis, "pod": POD, "tenant": TENANT, "agent": AGENT}],
        )

    def test_missing_handler_moves_all_current_ingress_to_dead_letter(self):
        self.redis.hset(self.registry, AGENT, "unknown")
        ingress = prefix(POD, TENANT, AGENT, "ingress")
        dead = prefix(POD, TENANT, AGENT, "dead")
        self.redis.rpush(ingress, b"first", b"second")
        dispatch_ingress(self.redis, pod=POD, tenant=TENANT, agent=AGENT)
        self.assertEqual(self.redis.eval_calls, 1)
        self.assertEqual(list(self.redis.lists[ingress]), [])
        self.assertEqual(list(self.redis.lists[dead]), [b"first", b"second"])

    def test_unroutable_drain_preserves_ingress_appended_after_atomic_snapshot(self):
        self.redis.hset(self.registry, AGENT, "unknown")
        ingress = prefix(POD, TENANT, AGENT, "ingress")
        dead = prefix(POD, TENANT, AGENT, "dead")
        self.redis.rpush(ingress, b"snapshot")
        self.redis.after_drain = lambda: self.redis.rpush(ingress, b"concurrent")
        dispatch_ingress(self.redis, pod=POD, tenant=TENANT, agent=AGENT)
        self.assertEqual(list(self.redis.lists[dead]), [b"snapshot"])
        self.assertEqual(list(self.redis.lists[ingress]), [b"concurrent"])

    def test_paused_destination_is_not_dispatched_or_dead_lettered(self):
        calls = []
        register_type("example", lambda **kwargs: calls.append(kwargs))
        self.redis.hset(self.registry, AGENT, "example")
        self.redis.set(prefix(POD, TENANT, AGENT, "paused"), "1")
        ingress = prefix(POD, TENANT, AGENT, "ingress")
        self.redis.rpush(ingress, "waiting")
        dispatch_ingress(self.redis, pod=POD, tenant=TENANT, agent=AGENT)
        self.assertEqual(calls, [])
        self.assertEqual(list(self.redis.lists[ingress]), ["waiting"])

    def test_register_override_unregister_and_reset_registry(self):
        first = lambda **kwargs: None
        second = lambda **kwargs: None
        register_type("example", first)
        self.assertIs(get_handler("example"), first)
        register_type("example", second)
        self.assertIs(get_handler("example"), second)
        unregister_type("example")
        self.assertIsNone(get_handler("example"))
        unregister_type("example")
        register_type("temporary", first)
        reset_registry()
        self.assertIsNone(get_handler("temporary"))

    def test_register_resolves_lazy_handler_and_rejects_invalid_handlers(self):
        register_type("lazy", (__name__, "sample_lazy_handler"))
        self.assertIs(get_handler("lazy"), sample_lazy_handler)
        for handler in (None, "not-callable", ("missing.dispatch.module", "handler")):
            with self.subTest(handler=handler):
                with self.assertRaisesRegex(ValueError, "invalid delivery handler"):
                    register_type("invalid", handler)

    def test_register_rejects_falsy_port_type_names(self):
        for name in ("", None, 0):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "port_type name must be non-empty"):
                    register_type(name, sample_lazy_handler)

    def test_delivery_kick_serializes_concurrent_dispatches_and_releases_tag(self):
        entered_first = threading.Event()
        release_first = threading.Event()
        state_lock = threading.Lock()
        calls = 0
        active = 0
        max_active = 0
        errors = []

        def handler(**kwargs):
            nonlocal calls, active, max_active
            with state_lock:
                calls += 1
                active += 1
                max_active = max(max_active, active)
                call_number = calls
            if call_number == 1:
                entered_first.set()
                if not release_first.wait(timeout=2):
                    raise AssertionError("test did not release first dispatch")
            with state_lock:
                active -= 1

        def kick():
            try:
                run_delivery_kick(AGENT, pod=POD, tenant=TENANT, r=self.redis)
            except BaseException as exc:
                errors.append(exc)

        register_type("example", handler)
        self.redis.hset(self.registry, AGENT, "example")
        first = threading.Thread(target=kick)
        second = threading.Thread(target=kick)
        first.start()
        self.assertTrue(entered_first.wait(timeout=1))
        second.start()
        try:
            self.assertTrue(self.redis.hsetnx_contention.wait(timeout=1))
            self.assertEqual(calls, 1, "second dispatch entered while busy tag was held")
        finally:
            release_first.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(calls, 2)
        self.assertEqual(max_active, 1)
        delivering = prefix(POD, TENANT, resource="delivering")
        self.assertIsNone(self.redis.hget(delivering, AGENT))


def sample_lazy_handler(**kwargs):
    return None


if __name__ == "__main__":
    unittest.main()

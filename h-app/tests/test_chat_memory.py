import os
import sys
import unittest
from pathlib import Path
from uuid import uuid4

import redis

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from lib.chat_memory import ChatMemory

POD = "testpod"


class ChatMemoryTests(unittest.TestCase):
    def setUp(self):
        self.redis = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
        try:
            self.redis.ping()
        except Exception:
            self.skipTest("real Redis server not available at REDIS_URL")
        self.tenant = f"chatmem-{uuid4().hex[:12]}"
        self.memory = ChatMemory(self.redis, POD, self.tenant, "bob", ttl_seconds_max=3600)

    def test_a_chat_id_never_written_reads_back_empty(self):
        self.assertEqual(self.memory.read_turns("never-seen"), [])

    def test_turns_read_back_oldest_first(self):
        self.memory.write_turn("chat-1", "user", "hi", 3600)
        self.memory.write_turn("chat-1", "assistant", "hello", 3600)
        self.memory.write_turn("chat-1", "user", "how are you", 3600)

        turns = self.memory.read_turns("chat-1")
        self.assertEqual([t["content"] for t in turns], ["hi", "hello", "how are you"])
        self.assertEqual([t["role"] for t in turns], ["user", "assistant", "user"])

    def test_different_chat_ids_are_isolated(self):
        self.memory.write_turn("chat-a", "user", "a-message", 3600)
        self.memory.write_turn("chat-b", "user", "b-message", 3600)

        self.assertEqual([t["content"] for t in self.memory.read_turns("chat-a")], ["a-message"])
        self.assertEqual([t["content"] for t in self.memory.read_turns("chat-b")], ["b-message"])

    def test_different_agents_are_isolated(self):
        bob_memory = self.memory
        alice_memory = ChatMemory(self.redis, POD, self.tenant, "alice", ttl_seconds_max=3600)

        bob_memory.write_turn("shared-chat-id", "user", "for bob", 3600)
        alice_memory.write_turn("shared-chat-id", "user", "for alice", 3600)

        self.assertEqual([t["content"] for t in bob_memory.read_turns("shared-chat-id")], ["for bob"])
        self.assertEqual([t["content"] for t in alice_memory.read_turns("shared-chat-id")], ["for alice"])

    def test_ttl_is_applied_to_the_turn_key(self):
        turn_key = self.memory.write_turn("chat-1", "user", "hi", 120)
        ttl = self.redis.ttl(turn_key)
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 120)

    def test_hot_keep_count_trims_the_index_to_the_most_recent_n(self):
        for i in range(5):
            self.memory.write_turn("chat-1", "user", f"turn-{i}", 3600, hot_keep_count=3)

        turns = self.memory.read_turns("chat-1")
        self.assertEqual([t["content"] for t in turns], ["turn-2", "turn-3", "turn-4"])

    def test_all_digit_chat_id_does_not_break_key_construction(self):
        # chat_id flows into a core.keys resource segment, which rejects
        # all-digit segments -- the "c"/"t" prefixes in _turn_key/_index_key
        # exist specifically so an all-digit chat_id or ts_ns doesn't hit
        # that rejection. A real caller (chat_id == `source`, an agent
        # name) can't itself be all-digit -- core.keys already forbids that
        # agent name entirely -- but nothing enforces it for chat_id here,
        # so this is a structural guarantee, not a "can't happen" argument.
        turn_key = self.memory.write_turn("1234567890", "user", "hi", 3600)
        self.assertIn("c1234567890", turn_key)
        self.assertEqual([t["content"] for t in self.memory.read_turns("1234567890")], ["hi"])

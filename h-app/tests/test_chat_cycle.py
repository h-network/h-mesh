import os
import sys
import unittest
from pathlib import Path
from uuid import uuid4

import redis

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from lib.chat_cycle import build_chat_prompt, run_chat_cycle
from lib.chat_memory import ChatMemory

POD = "testpod"


class BuildChatPromptTests(unittest.TestCase):
    def test_no_prior_turns_returns_the_message_unchanged(self):
        self.assertEqual(build_chat_prompt([], "hello"), "hello")

    def test_prior_turns_are_prefixed_as_a_transcript(self):
        turns = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        prompt = build_chat_prompt(turns, "how are you")
        self.assertIn("[user] hi", prompt)
        self.assertIn("[assistant] hello", prompt)
        self.assertTrue(prompt.endswith("Current message:\nhow are you\n"))


class RunChatCycleTests(unittest.TestCase):
    def setUp(self):
        self.redis = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
        try:
            self.redis.ping()
        except Exception:
            self.skipTest("real Redis server not available at REDIS_URL")
        tenant = f"chatcycle-{uuid4().hex[:12]}"
        self.memory = ChatMemory(self.redis, POD, tenant, "bob", ttl_seconds_max=3600)

    def test_first_call_for_a_chat_id_dispatches_the_message_unchanged(self):
        seen_prompts = []

        def dispatch(prompt):
            seen_prompts.append(prompt)
            return "reply-1"

        reply, prior_count = run_chat_cycle(
            self.memory, "chat-1", "hello", dispatch, ttl_seconds=3600
        )

        self.assertEqual(reply, "reply-1")
        self.assertEqual(prior_count, 0)
        self.assertEqual(seen_prompts, ["hello"])

    def test_second_call_includes_the_first_exchange_as_history(self):
        run_chat_cycle(self.memory, "chat-1", "hello", lambda p: "reply-1", ttl_seconds=3600)

        seen_prompts = []

        def dispatch(prompt):
            seen_prompts.append(prompt)
            return "reply-2"

        reply, prior_count = run_chat_cycle(
            self.memory, "chat-1", "second message", dispatch, ttl_seconds=3600
        )

        self.assertEqual(reply, "reply-2")
        self.assertEqual(prior_count, 2)
        self.assertIn("[user] hello", seen_prompts[0])
        self.assertIn("[assistant] reply-1", seen_prompts[0])
        self.assertIn("Current message:\nsecond message", seen_prompts[0])

    def test_both_turns_are_persisted_after_a_successful_dispatch(self):
        run_chat_cycle(self.memory, "chat-1", "hello", lambda p: "reply-1", ttl_seconds=3600)

        turns = self.memory.read_turns("chat-1")
        self.assertEqual([(t["role"], t["content"]) for t in turns], [
            ("user", "hello"), ("assistant", "reply-1"),
        ])

    def test_a_failed_dispatch_writes_no_turns(self):
        def dispatch(prompt):
            raise RuntimeError("dispatcher exploded")

        with self.assertRaises(RuntimeError):
            run_chat_cycle(self.memory, "chat-1", "hello", dispatch, ttl_seconds=3600)

        self.assertEqual(self.memory.read_turns("chat-1"), [])

    def test_a_chat_id_that_was_never_reused_looks_exactly_like_a_one_off(self):
        # Two different chat_ids, each used exactly once: neither call sees
        # the other's turn, and each prompt is byte-identical to the raw
        # message -- the whole point being there's no separate "is this a
        # one-off" code path to get wrong.
        seen_prompts = []

        def dispatch(prompt):
            seen_prompts.append(prompt)
            return "ok"

        run_chat_cycle(self.memory, "chat-a", "message-a", dispatch, ttl_seconds=3600)
        run_chat_cycle(self.memory, "chat-b", "message-b", dispatch, ttl_seconds=3600)

        self.assertEqual(seen_prompts, ["message-a", "message-b"])

"""Checks how model calls ride out a lost connection, without contacting any service."""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx
from openai import APIConnectionError

from research_agent import llm as llm_module
from research_agent.llm import CONNECTION_WAITS, LLM

CFG = {"api": {"base_url": "https://example.invalid/v1", "key_env": "TEST_KEY",
               "app_name": "test"},
       "budget": {"daily_usd": 100, "monthly_usd": 1000}, "pricing": {}}


class FakeStore:
    def spend_since(self, day):
        return 0.0

    def add_cost(self, *args):
        pass


def ok_response():
    message = SimpleNamespace(content="hi")
    parsed = SimpleNamespace(usage=None, choices=[SimpleNamespace(message=message)])
    return SimpleNamespace(parse=lambda: parsed, headers={"x-litellm-response-cost": "0"})


def lost():
    return APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))


class ConnectionRetryTest(unittest.TestCase):
    def setUp(self):
        with mock.patch.dict(os.environ, {"TEST_KEY": "x"}):
            self.llm = LLM(CFG, FakeStore(), "test")
        self.create = mock.Mock()
        self.llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            with_raw_response=SimpleNamespace(create=self.create))))
        self.sleeps = []
        patcher = mock.patch.object(llm_module.time, "sleep", self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self):
        return self.llm.chat("test", "m", [{"role": "user", "content": "x"}])

    def test_waits_out_a_short_outage(self):
        self.create.side_effect = [lost(), lost(), ok_response()]
        self.assertEqual(self.call().content, "hi")
        self.assertEqual(self.sleeps, CONNECTION_WAITS[:2])
        self.assertFalse(self.llm.offline)

    def test_gives_up_within_a_minute_then_fails_fast(self):
        self.create.side_effect = lost()
        with self.assertRaises(APIConnectionError):
            self.call()
        self.assertEqual(self.sleeps, CONNECTION_WAITS)
        self.assertLessEqual(sum(self.sleeps), 60)
        self.assertTrue(self.llm.offline)
        # The next call tries once, without waiting, so the run can finish.
        self.sleeps.clear()
        with self.assertRaises(APIConnectionError):
            self.call()
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.llm.connection_failures, 2)

    def test_connection_back_resets_offline(self):
        self.llm.offline = True
        self.create.side_effect = [ok_response()]
        self.call()
        self.assertFalse(self.llm.offline)


if __name__ == "__main__":
    unittest.main()

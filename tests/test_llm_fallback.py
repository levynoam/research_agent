"""Checks what happens when a model is at capacity (HTTP 429), without contacting any service."""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx
from openai import APIStatusError, RateLimitError

from research_agent import llm as llm_module
from research_agent.llm import CAPACITY_COOLDOWN, CAPACITY_ROUNDS, LLM, ModelsUnavailable

CFG = {"api": {"base_url": "https://example.invalid/v1", "key_env": "TEST_KEY",
               "app_name": "test"},
       "budget": {"daily_usd": 100, "monthly_usd": 1000},
       "pricing": {"default": {"input": 1, "output": 1}},
       "fallback_models": ["sonnet", "sol"]}


class FakeStore:
    def __init__(self):
        self.costs = []

    def spend_since(self, day):
        return 0.0

    def add_cost(self, run_id, stage, model, tin, tout, usd, fallback_for=None):
        self.costs.append((model, fallback_for))


def ok_response():
    message = SimpleNamespace(content="hi")
    parsed = SimpleNamespace(usage=None, choices=[SimpleNamespace(message=message)])
    return SimpleNamespace(parse=lambda: parsed, headers={"x-litellm-response-cost": "0"})


def status_error(cls, code, headers=None):
    request = httpx.Request("POST", "https://example.invalid")
    return cls("refused", response=httpx.Response(code, request=request, headers=headers or {}),
               body=None)


class FallbackTest(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        with mock.patch.dict(os.environ, {"TEST_KEY": "x"}):
            self.llm = LLM(CFG, self.store, "test")
        self.busy = set()      # models that answer 429 right now
        self.asked = []        # every model that was sent a request, in order
        self.clock = [1000.0]  # sleeping moves this clock instead of waiting

        def create(model, **kwargs):
            self.asked.append(model)
            if model in self.busy:
                raise status_error(RateLimitError, 429)
            return ok_response()

        self.llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            with_raw_response=SimpleNamespace(create=create))))
        for name, fake in [("sleep", lambda s: self.clock.__setitem__(0, self.clock[0] + s)),
                           ("monotonic", lambda: self.clock[0])]:
            patcher = mock.patch.object(llm_module.time, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, model="opus"):
        return self.llm.chat("stage", model, [{"role": "user", "content": "x"}])

    def test_uses_the_wanted_model_when_it_has_room(self):
        self.call()
        self.assertEqual(self.asked, ["opus"])
        self.assertEqual(self.store.costs, [("opus", None)])

    def test_falls_back_in_order_and_records_who_answered(self):
        self.busy = {"opus"}
        self.assertEqual(self.call().content, "hi")
        self.assertEqual(self.asked, ["opus", "sonnet"])
        self.assertEqual(self.store.costs, [("sonnet", "opus")])
        self.busy = {"opus", "sonnet"}
        self.asked.clear()
        self.clock[0] += CAPACITY_COOLDOWN + 1
        self.call()
        self.assertEqual(self.asked, ["opus", "sonnet", "sol"])
        self.assertEqual(self.store.costs[-1], ("sol", "opus"))

    def test_leaves_a_busy_model_alone_for_a_minute_then_returns_to_it(self):
        self.busy = {"opus"}
        self.call()
        self.busy = set()
        self.asked.clear()
        self.call()                      # within the minute: straight to the fallback
        self.assertEqual(self.asked, ["sonnet"])
        self.clock[0] += CAPACITY_COOLDOWN + 1
        self.call()                      # afterwards the wanted model is tried again
        self.assertEqual(self.asked, ["sonnet", "opus"])

    def test_a_step_whose_own_model_is_a_fallback_skips_it(self):
        self.busy = {"sonnet"}
        self.call("sonnet")
        self.assertEqual(self.asked, ["sonnet", "sol"])

    def test_waits_when_all_are_busy_and_takes_the_first_that_returns(self):
        self.busy = {"opus", "sonnet", "sol"}
        started = self.clock[0]

        def free_sonnet_later(model, **kwargs):
            if self.clock[0] - started >= CAPACITY_COOLDOWN:
                self.busy.discard("sonnet")
            self.asked.append(model)
            if model in self.busy:
                raise status_error(RateLimitError, 429)
            return ok_response()

        self.llm.client.chat.completions.with_raw_response.create = free_sonnet_later
        self.call()
        self.assertEqual(self.asked, ["opus", "sonnet", "sol", "opus", "sonnet"])

    def test_stops_the_run_when_every_model_stays_busy(self):
        self.busy = {"opus", "sonnet", "sol"}
        with self.assertRaises(ModelsUnavailable) as caught:
            self.call()
        self.assertEqual(len(self.asked), 3 * CAPACITY_ROUNDS)
        self.assertLessEqual(self.clock[0] - 1000.0, 3 * 60)   # gives up within minutes
        self.assertIn("opus is at capacity", str(caught.exception))
        self.assertEqual(self.store.costs, [])

    def test_follows_the_wait_the_service_asks_for(self):
        def create(model, **kwargs):
            self.asked.append(model)
            if model == "opus":
                raise status_error(RateLimitError, 429, {"retry-after": "20"})
            return ok_response()

        self.llm.client.chat.completions.with_raw_response.create = create
        self.call()
        self.clock[0] += 21
        self.asked.clear()
        self.call()
        self.assertEqual(self.asked[0], "opus")

    def test_a_refused_request_is_not_sent_to_other_models(self):
        def create(model, **kwargs):
            self.asked.append(model)
            raise status_error(APIStatusError, 400)

        self.llm.client.chat.completions.with_raw_response.create = create
        with self.assertRaises(APIStatusError):
            self.call()
        self.assertEqual(set(self.asked), {"opus"})

    def test_a_model_that_keeps_failing_is_replaced_too(self):
        def create(model, **kwargs):
            self.asked.append(model)
            if model == "opus":
                raise status_error(APIStatusError, 503)
            return ok_response()

        self.llm.client.chat.completions.with_raw_response.create = create
        self.call()
        self.assertEqual(self.asked, ["opus", "opus", "opus", "sonnet"])
        self.assertEqual(self.store.costs, [("sonnet", "opus")])


if __name__ == "__main__":
    unittest.main()

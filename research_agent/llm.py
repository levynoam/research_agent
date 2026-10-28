"""Calls models on NVIDIA Inference Hub and records what each call cost."""

import base64
import json
import logging
import os
import re
import time

import httpx
from openai import APIError, OpenAI

log = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    pass


class LLM:
    def __init__(self, cfg: dict, store, run_date: str):
        api = cfg["api"]
        key = os.environ.get(api["key_env"])
        if not key:
            raise RuntimeError(f"Environment variable {api['key_env']} is not set")
        self._key = key
        self.base_url = api["base_url"]
        self.client = OpenAI(base_url=self.base_url, api_key=key, max_retries=3, timeout=300,
                             default_headers={"x-nv-application": api["app_name"]})
        self.cfg = cfg
        self.store = store
        self.run_date = run_date

    # --- budget -------------------------------------------------------------
    def check_budget(self):
        from datetime import date

        today = date.today()
        b = self.cfg["budget"]
        if self.store.spend_since(today) >= b["daily_usd"]:
            raise BudgetExceeded(f"daily budget ${b['daily_usd']} reached")
        if self.store.spend_since(today.replace(day=1)) >= b["monthly_usd"]:
            raise BudgetExceeded(f"monthly budget ${b['monthly_usd']} reached")

    def _fallback_cost(self, model, tin, tout) -> float:
        prices = self.cfg["pricing"].get(model) or self.cfg["pricing"]["default"]
        return (tin * prices["input"] + tout * prices["output"]) / 1e6

    # --- chat ---------------------------------------------------------------
    def chat(self, stage: str, model: str, messages: list[dict], max_tokens: int = 4000,
             tools: list[dict] | None = None, tool_choice=None):
        """One model call. Returns the response message object."""
        self.check_budget()
        kwargs = dict(model=model, messages=messages, max_tokens=max_tokens)
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        for attempt in range(3):
            try:
                raw = self.client.chat.completions.with_raw_response.create(**kwargs)
                break
            except APIError as e:
                if attempt == 2:
                    raise
                log.warning("model call failed (%s), retrying", e)
                time.sleep(5 * (attempt + 1))
        resp = raw.parse()

        tin = resp.usage.prompt_tokens if resp.usage else 0
        tout = resp.usage.completion_tokens if resp.usage else 0
        cost_header = raw.headers.get("x-litellm-response-cost")
        usd = float(cost_header) if cost_header else self._fallback_cost(model, tin, tout)
        self.store.add_cost(self.run_date, stage, model, tin, tout, usd)
        return resp.choices[0].message

    def chat_json(self, stage: str, model: str, system: str, user, max_tokens: int = 4000):
        """Model call whose answer must be JSON. Retries once if the JSON is broken.

        `user` is a string, or a list of content parts (e.g. text plus an image).
        """
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for attempt in range(2):
            msg = self.chat(stage, model, messages, max_tokens=max_tokens)
            try:
                return parse_json(msg.content or "")
            except ValueError:
                if attempt == 1:
                    raise
                messages += [{"role": "assistant", "content": msg.content or ""},
                             {"role": "user", "content": "That was not valid JSON. "
                              "Reply again with only the JSON, nothing else."}]

    # --- image generation --------------------------------------------------
    def generate_image(self, model: str, prompt: str, size: str) -> bytes:
        """Returns PNG bytes."""
        self.check_budget()
        raw = self.client.images.with_raw_response.generate(model=model, prompt=prompt,
                                                             size=size, n=1)
        resp = raw.parse()
        cost = raw.headers.get("x-litellm-response-cost")
        usd = float(cost) if cost else self.cfg["pricing"]["image_per_call"]
        self.store.add_cost(self.run_date, "figures", model, 0, 0, usd)
        data = resp.data[0]
        if data.b64_json:
            return base64.b64decode(data.b64_json)
        r = httpx.get(data.url, timeout=60)  # some providers return a link instead
        r.raise_for_status()
        return r.content

    # --- web search (also billed through the Hub) ---------------------------
    def web_search(self, query: str, max_results: int = 5) -> list[dict]:
        self.check_budget()
        r = httpx.post(f"{self.base_url}/search/perplexity-search",
                       headers={"Authorization": f"Bearer {self._key}"},
                       json={"query": query, "max_results": max_results}, timeout=60)
        r.raise_for_status()
        cost = r.headers.get("x-litellm-response-cost")
        usd = float(cost) if cost else self.cfg["pricing"]["web_search_per_call"]
        self.store.add_cost(self.run_date, "research", "perplexity-search", 0, 0, usd)
        return r.json().get("results", [])


def parse_json(text: str):
    """Pull the JSON object or list out of a model reply (tolerates ```json fences)."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if starts:
        start = min(starts)
        end = max(text.rfind("}"), text.rfind("]"))
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError("no valid JSON in model reply")

"""Calls models on NVIDIA Inference Hub and records what each call cost."""

import base64
import json
import logging
import os
import re
import time

import httpx
from openai import APIConnectionError, APIError, APIStatusError, OpenAI, RateLimitError

log = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    pass


class ModelsUnavailable(Exception):
    """No model could take a call: the wanted one and every fallback were at capacity or
    failing. The run stops here rather than leave papers out; retrying it later continues
    from the work already finished."""


# Waits after the model service can't be reached at all: enough to ride out a short
# Wi-Fi blip. A longer outage (say, the lid closed on the move) isn't waited out: the run
# finishes with what it has, is marked incomplete, and the web app resumes it once the
# connection is back.
CONNECTION_WAITS = [10, 30]

# A model at capacity answers HTTP 429 at once: the service keeps a budget of tokens per
# minute for each model, shared by all its users, and doesn't queue. Such a model is left
# alone for a minute while the fallback models (config: fallback_models) take the calls.
# If every model in the chain is busy, the call waits for the first one to come back, and
# after this many rounds over the whole chain (about two minutes) the run stops.
CAPACITY_COOLDOWN = 60
CAPACITY_ROUNDS = 3


def _short(model: str) -> str:
    return model.rsplit("/", 1)[-1].removeprefix("eccn-")


class LLM:
    def __init__(self, cfg: dict, store, run_id: str):
        api = cfg["api"]
        key = os.environ.get(api["key_env"])
        if not key:
            raise RuntimeError(f"Environment variable {api['key_env']} is not set")
        self._key = key
        self.base_url = api["base_url"]
        # max_retries=0: retries and fallbacks are decided here, not inside the client
        # (which would wait a minute, three times over, on every "at capacity" answer).
        self.client = OpenAI(base_url=self.base_url, api_key=key, max_retries=0, timeout=300,
                             default_headers={"x-nv-application": api["app_name"]})
        self.fallbacks = list(cfg.get("fallback_models") or [])
        self.busy: dict[str, tuple[float, str]] = {}  # model -> (busy until, why)
        self.cfg = cfg
        self.store = store
        self.run_id = run_id
        # Set once a call gave up on reaching the service. Later calls then try once each
        # without waiting, so the run ends quickly with what it has.
        self.offline = False
        self.connection_failures = 0  # calls that failed because the service was unreachable

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
        """One model call. Returns the response message object.

        If the model is at capacity, a fallback model answers instead (see _create).
        """
        self.check_budget()
        kwargs = dict(messages=messages, max_tokens=max_tokens)
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        raw, used = self._create(model, kwargs)
        resp = raw.parse()

        tin = resp.usage.prompt_tokens if resp.usage else 0
        tout = resp.usage.completion_tokens if resp.usage else 0
        cost_header = raw.headers.get("x-litellm-response-cost")
        usd = float(cost_header) if cost_header else self._fallback_cost(used, tin, tout)
        self.store.add_cost(self.run_id, stage, used, tin, tout, usd,
                            fallback_for=model if used != model else None)
        return resp.choices[0].message

    def _create(self, model: str, kwargs: dict):
        """Sends the call to `model`, or to the first fallback model that can take it.

        Returns (raw response, the model that answered). Raises ModelsUnavailable when
        every model in the chain stayed at capacity (or kept failing) for all rounds.
        """
        chain = [model] + [m for m in self.fallbacks if m != model]
        for round_no in range(CAPACITY_ROUNDS):
            for m in chain:
                if time.monotonic() < self.busy.get(m, (0, ""))[0]:
                    continue
                try:
                    raw = self._create_one(m, kwargs)
                except RateLimitError as e:
                    why, wait = "at capacity", _retry_after(e)
                except APIStatusError as e:
                    if e.status_code < 500:
                        raise  # the request itself was refused; another model won't fix that
                    why, wait = f"failing (HTTP {e.status_code})", CAPACITY_COOLDOWN
                else:
                    if m != model:
                        log.debug("%s answered instead of %s", _short(m), _short(model))
                    return raw, m
                self.busy[m] = (time.monotonic() + wait, why)
                rest = [_short(x) for x in chain[chain.index(m) + 1:]]
                log.warning("%s is %s; leaving it alone for %d s%s", _short(m), why, wait,
                            f", using {' then '.join(rest)} meanwhile" if rest else "")
            if round_no + 1 < CAPACITY_ROUNDS:
                wait = max(1.0, min(self.busy[m][0] for m in chain) - time.monotonic())
                log.warning("every model is busy (%s); waiting %d s",
                            ", ".join(_short(m) for m in chain), wait)
                time.sleep(wait)
        raise ModelsUnavailable("no model could take the call: " + "; ".join(
            f"{_short(m)} is {self.busy[m][1]}" for m in chain))

    def _create_one(self, model: str, kwargs: dict):
        """One model, with the retries that make sense for it: short waits on a lost
        connection or a server error, none on "at capacity" (the caller falls back)."""
        attempt = 0
        while True:
            try:
                raw = self.client.chat.completions.with_raw_response.create(model=model,
                                                                             **kwargs)
                self.offline = False
                return raw
            except APIConnectionError as e:  # no answer at all, including timeouts
                waits = [] if self.offline else CONNECTION_WAITS
                if attempt >= len(waits):
                    self.offline = True
                    self.connection_failures += 1
                    raise
                log.warning("can't reach the model service (%s); retrying in %d s",
                            e, waits[attempt])
                time.sleep(waits[attempt])
            except RateLimitError:
                raise
            except APIError as e:  # the service answered with an error
                if attempt >= 2:
                    raise
                log.warning("model call failed (%s), retrying", e)
                time.sleep(5 * (attempt + 1))
            attempt += 1

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
        client = self.client.with_options(max_retries=3)  # no fallback for images
        raw = client.images.with_raw_response.generate(model=model, prompt=prompt,
                                                        size=size, n=1)
        resp = raw.parse()
        cost = raw.headers.get("x-litellm-response-cost")
        usd = float(cost) if cost else self.cfg["pricing"]["image_per_call"]
        self.store.add_cost(self.run_id, "figures", model, 0, 0, usd)
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
        self.store.add_cost(self.run_id, "research", "perplexity-search", 0, 0, usd)
        return r.json().get("results", [])


def _retry_after(e: RateLimitError) -> float:
    """How long to leave a model alone after a 429: what the service asks, within reason."""
    try:
        asked = float(e.response.headers.get("retry-after", ""))
    except (AttributeError, TypeError, ValueError):
        return CAPACITY_COOLDOWN
    return min(max(asked, 5.0), 300.0)


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

"""Provider adapters. The default is the local fake provider (no key, no cost).

Every adapter turns transport and HTTP errors into a ProviderError that says
whether the call may have been charged (see retry.py).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

from .config import Settings
from .retry import NOT_CHARGED, UNKNOWN, ProviderError, parse_retry_after


@dataclass
class Generation:
    text: str
    input_tokens: int
    output_tokens: int
    cost_micros: int
    replayed: bool = False      # the provider returned a stored reply for this idempotency key


class HttpProvider:
    name = "http"
    dedupes = False             # does the provider honour an Idempotency-Key header?

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        # httpx timeouts apply per phase (connect, write, each read), not to the whole
        # call, so each phase gets half of PROVIDER_TIMEOUT and _post adds an overall
        # deadline. A call therefore ends within 2 * PROVIDER_TIMEOUT, inside the lease.
        half = settings.provider_timeout_s / 2
        self.client = httpx.Client(timeout=httpx.Timeout(half), transport=transport)

    def close(self) -> None:
        self.client.close()

    def _send(self, url: str, json: dict, headers: dict) -> httpx.Response:
        """POST and read the whole reply, giving up once PROVIDER_TIMEOUT has passed.

        The deadline is checked between chunks of the reply, so a reply that trickles
        in slowly cannot keep the call alive past the lease.
        """
        deadline = time.monotonic() + self.settings.provider_timeout_s
        with self.client.stream("POST", url, json=json, headers=headers) as resp:
            body = []
            for chunk in resp.iter_bytes():
                body.append(chunk)
                if time.monotonic() > deadline:
                    raise httpx.ReadTimeout("overall deadline passed", request=resp.request)
            # The body is already decoded: drop the headers that describe the wire format.
            headers = [(k, v) for k, v in resp.headers.multi_items()
                       if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
            return httpx.Response(resp.status_code, headers=headers, content=b"".join(body),
                                  request=resp.request)

    def _post(self, url: str, json: dict, headers: dict) -> httpx.Response:
        try:
            resp = self._send(url, json, headers)
        except httpx.ConnectError as e:            # never reached the provider
            raise ProviderError("connect_failed", str(e) or "could not connect", NOT_CHARGED) from e
        except httpx.ConnectTimeout as e:
            raise ProviderError("connect_failed", "connect timeout", NOT_CHARGED) from e
        except httpx.TimeoutException as e:        # sent, no reply in time: may have been charged
            raise ProviderError("timeout", f"no reply within {self.settings.provider_timeout_s}s", UNKNOWN) from e
        except httpx.TransportError as e:          # connection dropped mid-request
            raise ProviderError("connection_lost", str(e) or type(e).__name__, UNKNOWN) from e
        if resp.status_code == 429:
            raise ProviderError("rate_limited", "429 Too Many Requests", NOT_CHARGED, 429,
                                parse_retry_after(resp.headers.get("retry-after")))
        if resp.status_code >= 500:
            raise ProviderError("server_error", f"{resp.status_code} from provider", NOT_CHARGED, resp.status_code,
                                parse_retry_after(resp.headers.get("retry-after")))
        if resp.status_code in (401, 403):
            raise ProviderError("auth", f"{resp.status_code}: check the API key", NOT_CHARGED, resp.status_code)
        if resp.status_code >= 400:
            raise ProviderError("bad_request", f"{resp.status_code}: {resp.text[:200]}", NOT_CHARGED, resp.status_code)
        return resp

    def _cost(self, tin: int, tout: int) -> int:
        return tin * self.settings.price_in_micros + tout * self.settings.price_out_micros

    def generate(self, prompt: str, max_tokens: int, idempotency_key: str, request_ref: str = "") -> Generation:
        raise NotImplementedError


class FakeProvider(HttpProvider):
    """Client for fakeprovider/ (deliberately unreliable, deduplicates by Idempotency-Key)."""
    name = "fake"
    dedupes = True

    def generate(self, prompt, max_tokens, idempotency_key, request_ref=""):
        headers = {"Idempotency-Key": idempotency_key}
        if request_ref:
            headers["X-Request-Ref"] = request_ref   # only used by the chaos report to group charges
        resp = self._post(f"{self.settings.fake_provider_url}/v1/generate",
                          {"prompt": prompt, "max_tokens": max_tokens}, headers)
        body = resp.json()
        return Generation(body["text"], body["usage"]["input_tokens"], body["usage"]["output_tokens"],
                          body["usage"]["cost_micros"], bool(body.get("replayed")))


class OpenAIProvider(HttpProvider):
    """OpenAI Chat Completions over plain HTTP. Not run in this repo (no key); see Limits."""
    name = "openai"
    dedupes = False

    def generate(self, prompt, max_tokens, idempotency_key, request_ref=""):
        key = os.environ.get("OPENAI_API_KEY", "")
        resp = self._post(
            "https://api.openai.com/v1/chat/completions",
            {"model": self.settings.llm_model, "max_completion_tokens": max_tokens,
             "messages": [{"role": "user", "content": prompt}]},
            {"Authorization": f"Bearer {key}"},
        )
        body = resp.json()
        tin, tout = body["usage"]["prompt_tokens"], body["usage"]["completion_tokens"]
        return Generation(body["choices"][0]["message"]["content"] or "", tin, tout, self._cost(tin, tout))


class AnthropicProvider(HttpProvider):
    """Anthropic Messages API over plain HTTP. Not run in this repo (no key); see Limits."""
    name = "anthropic"
    dedupes = False

    def generate(self, prompt, max_tokens, idempotency_key, request_ref=""):
        resp = self._post(
            "https://api.anthropic.com/v1/messages",
            {"model": self.settings.llm_model, "max_tokens": max_tokens,
             "messages": [{"role": "user", "content": prompt}]},
            {"x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01"},
        )
        body = resp.json()
        text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
        tin, tout = body["usage"]["input_tokens"], body["usage"]["output_tokens"]
        return Generation(text, tin, tout, self._cost(tin, tout))


def make_provider(settings: Settings, transport: httpx.BaseTransport | None = None) -> HttpProvider:
    cls = {"fake": FakeProvider, "openai": OpenAIProvider, "anthropic": AnthropicProvider}[settings.provider]
    if cls is not FakeProvider and not settings.llm_model:
        raise ValueError("Set LLM_MODEL to a model id from your provider's documentation")
    return cls(settings, transport)

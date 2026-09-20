"""One OpenAI-compatible HTTP adapter and a clearly labelled fixture adapter."""
import json
import os
import urllib.request
import urllib.error
import urllib.parse
from .contracts import Fault


class RecordedProvider:
    def __init__(self, tape):
        self.tape = tape

    def complete(self, request, key, index):
        rows = self.tape.get(key, [])
        if index >= len(rows):
            raise Fault("recording_exhausted", key)
        value = rows[index]
        def resolve(value):
            if isinstance(value, dict) and set(value) == {"$input"}:
                obj = json.loads(request["messages"][-1]["content"])
                for part in value["$input"].split("."):
                    obj = obj[int(part)] if isinstance(obj,list) else obj[part]
                return obj
            if isinstance(value, dict):
                return {k:resolve(v) for k,v in value.items()}
            if isinstance(value, list):
                return [resolve(v) for v in value]
            return value
        value = resolve(value)
        return {"text": value if isinstance(value,str) else json.dumps(value), "usage": None, "provider": "recorded"}


class HTTPProvider:
    def __init__(self, config):
        self.config = config
        url = urllib.parse.urlsplit(config.get("base_url", ""))
        if url.scheme not in ["http", "https"] or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise Fault("endpoint_invalid")
        if not config.get("model"):
            raise Fault("model_missing")

    def complete(self, request, key, index):
        cfg = self.config
        data = {"model": cfg["model"], "messages": request["messages"],
                "temperature": cfg.get("temperature", 0), "max_tokens": request["max_tokens"]}
        if cfg.get("json_mode", True):
            data["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        secret = os.environ.get(cfg.get("api_key_env", "LOCALMEDBOT_API_KEY"), "")
        if secret:
            headers["Authorization"] = "Bearer " + secret
        req = urllib.request.Request(cfg["base_url"].rstrip("/") + "/chat/completions", data=json.dumps(data).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=request["timeout"]) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise Fault("response_too_large")
                result = json.loads(raw)
            choice = result["choices"][0]
            if choice.get("finish_reason") == "length":
                raise Fault("output_truncated")
            text = choice["message"]["content"]
            if not isinstance(text, str):
                raise Fault("provider_shape")
            return {"text": text, "usage": result.get("usage"), "provider": "http"}
        except urllib.error.HTTPError as exc:
            # Never record response bodies, which can contain upstream secrets.
            raise Fault("transport_retryable" if exc.code in [408,429] or exc.code >= 500 else "provider_rejected", str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Fault("transport_retryable") from None
        except (ValueError,KeyError,IndexError,TypeError):
            raise Fault("provider_shape") from None


def make_provider(config, tape=None):
    if not isinstance(config.get("max_tokens",2048),int) or not 1 <= config.get("max_tokens",2048) <= 65536 or not 0 < config.get("timeout",45) <= 600:
        raise Fault("model_limits")
    if config.get("provider") == "recorded":
        return RecordedProvider(tape or {})
    if config.get("provider") == "http":
        return HTTPProvider(config)
    raise Fault("unknown_provider")

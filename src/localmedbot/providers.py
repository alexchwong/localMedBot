"""Recorded, OpenRouter/LM Studio OpenAI-compatible, and self executors."""
from __future__ import annotations
import json
import socket
import time
import urllib.error
import urllib.request
from .contracts import Fault, ModelSuspension


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Fault("provider_redirect_rejected", str(code))


class RecordedProvider:
    def __init__(self, tape: dict): self.tape=tape
    def complete(self, request: dict, key: str, index: int) -> dict:
        rows=self.tape.get(key,[])
        if isinstance(rows,dict):
            pair=f"{request.get('attempt')}:{request.get('call_index')}"
            if pair not in rows:
                raise Fault("recording_exhausted",f"{key}:{pair}")
            value=rows[pair]
        else:
            if index >= len(rows):
                raise Fault("recording_exhausted",key)
            value=rows[index]
        def resolve(v):
            if isinstance(v,dict) and set(v)=={"$input"}:
                obj=json.loads(request["messages"][-1]["content"])
                for part in v["$input"].split("."):
                    obj=obj[int(part)] if isinstance(obj,list) else obj[part]
                return obj
            if isinstance(v,dict): return {k:resolve(x) for k,x in v.items()}
            if isinstance(v,list): return [resolve(x) for x in v]
            return v
        value=resolve(value)
        return {"text":value if isinstance(value,str) else json.dumps(value),"usage":None,"returned_model":None,"finish_reason":"stop","provider_request_id":None,"executor":"recorded","duration":0.0}


class SelfProvider:
    def complete(self, request: dict, key: str, index: int) -> dict:
        raise ModelSuspension(request["request_id"])


class HTTPProvider:
    def __init__(self, profile: dict, credential: str | None):
        self.profile,self.credential=profile,credential
        self.opener=urllib.request.build_opener(_NoRedirect())

    def complete(self, request: dict, key: str, index: int) -> dict:
        role=request["role"]
        settings=self.profile["effective_roles"][role]
        data={"model":settings["model"],"messages":request["messages"],"temperature":settings.get("temperature",0),"max_tokens":settings.get("max_tokens",4096)}
        if settings.get("json_mode",True): data["response_format"]={"type":"json_object"}
        headers={"Content-Type":"application/json"}
        if self.credential: headers["Authorization"]="Bearer "+self.credential
        req=urllib.request.Request(self.profile["base_url"].rstrip("/")+"/chat/completions",data=json.dumps(data).encode("utf-8"),headers=headers)
        start=time.monotonic()
        try:
            with self.opener.open(req,timeout=request["timeout"]) as response:
                raw=response.read(2_000_001)
                if len(raw)>2_000_000: raise Fault("provider_incompatible_response","response_too_large")
                result=json.loads(raw)
                request_id=response.headers.get("x-request-id") or response.headers.get("x-openrouter-request-id")
            choice=result["choices"][0]
            finish=choice.get("finish_reason")
            if finish=="length": raise Fault("output_truncated")
            text=choice["message"]["content"]
            if not isinstance(text,str) or len(text.encode("utf-8"))>100_000: raise Fault("provider_incompatible_response")
            return {"text":text,"usage":result.get("usage"),"returned_model":result.get("model"),"finish_reason":finish,
                    "provider_request_id":request_id,"executor":self.profile["executor"],"duration":time.monotonic()-start}
        except Fault: raise
        except urllib.error.HTTPError as exc:
            if exc.code in (401,403): code="authentication_rejected"
            elif exc.code in (408,429) or exc.code>=500: code="provider_transient_failure"
            else: code="provider_request_rejected"
            raise Fault(code,str(exc.code)) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            reason=getattr(exc,"reason",None)
            if isinstance(reason,(TimeoutError,socket.timeout)): raise Fault("provider_timeout") from None
            raise Fault("endpoint_unreachable") from None
        except (ValueError,KeyError,IndexError,TypeError):
            raise Fault("provider_incompatible_response") from None


def make_provider(profile: dict, credential: str | None = None, tape: dict | None = None):
    executor=profile["executor"]
    if executor=="recorded": return RecordedProvider(tape or {})
    if executor=="self": return SelfProvider()
    if executor in {"openrouter","lmstudio"}:
        if executor=="openrouter" and not credential: raise Fault("credential_missing")
        return HTTPProvider(profile,credential)
    raise Fault("unknown_provider")

"""Recorded, OpenRouter/LM Studio, and self model executors."""
from __future__ import annotations
import json
import socket
import time
import urllib.error
import urllib.request
from .contracts import Fault, ModelSuspension


_OPENROUTER_REASONING={"default","none","minimal","low","medium","high","xhigh"}
_LMSTUDIO_REASONING={"default","none","low","medium","high"}


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
                # Repair requests append failed output and feedback after the original
                # task message. Recorded fixtures always resolve placeholders against
                # that frozen original model input, not the repair envelope.
                obj=None
                for message in request.get("messages",[]):
                    if message.get("role")!="user": continue
                    try:
                        candidate=json.loads(message.get("content", ""))
                    except (TypeError,ValueError):
                        continue
                    if isinstance(candidate,(dict,list)):
                        obj=candidate; break
                if obj is None: raise Fault("recording_input_unavailable")
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
    """Provider-aware HTTP adapter with deterministic validation left to localMedBot."""
    def __init__(self, profile: dict, credential: str | None):
        self.profile,self.credential=profile,credential
        self.opener=urllib.request.build_opener(_NoRedirect())

    def _headers(self):
        headers={"Content-Type":"application/json"}
        if self.credential: headers["Authorization"]="Bearer "+self.credential
        return headers

    def _post(self, endpoint: str, data: dict, timeout: float):
        req=urllib.request.Request(endpoint,data=json.dumps(data).encode("utf-8"),headers=self._headers(),method="POST")
        with self.opener.open(req,timeout=timeout) as response:
            raw=response.read(2_000_001)
            if len(raw)>2_000_000: raise Fault("provider_incompatible_response","response_too_large")
            result=json.loads(raw)
            request_id=response.headers.get("x-request-id") or response.headers.get("x-openrouter-request-id")
        if not isinstance(result,dict): raise Fault("provider_incompatible_response")
        return result,request_id

    def _error_detail(self, exc: urllib.error.HTTPError, endpoint: str) -> str:
        try: raw=exc.read(1001)
        except OSError: raw=b""
        text=raw[:1000].decode("utf-8",errors="replace").strip()
        if self.credential and text: text=text.replace(self.credential,"[redacted]")
        suffix=f": {text}" if text else ""
        return f"HTTP {exc.code} at {endpoint}{suffix}"

    def _raise_http(self, exc: urllib.error.HTTPError, endpoint: str):
        if exc.code in (401,403): code="authentication_rejected"
        elif exc.code in (408,429) or exc.code>=500: code="provider_transient_failure"
        else: code="provider_request_rejected"
        raise Fault(code,self._error_detail(exc,endpoint)) from None

    @staticmethod
    def _chat_payload(settings, messages):
        data={"model":settings["model"],"messages":messages,"temperature":settings.get("temperature",0),"max_tokens":settings.get("max_tokens",4096),"stream":False}
        reasoning=str(settings.get("reasoning","default")).strip().lower()
        if reasoning!="default": data["reasoning"]={"effort":reasoning}
        return data

    @staticmethod
    def _chat_result(result):
        choice=result["choices"][0]
        finish=choice.get("finish_reason")
        if finish=="length": raise Fault("output_truncated")
        text=choice["message"]["content"]
        return text,finish,result.get("usage"),result.get("model"),result.get("id")

    @staticmethod
    def _responses_result(result):
        parts=[]
        for item in result.get("output",[]):
            if not isinstance(item,dict) or item.get("type")!="message": continue
            for part in item.get("content",[]) if isinstance(item.get("content"),list) else []:
                if isinstance(part,dict) and part.get("type") in {"output_text","text"} and isinstance(part.get("text"),str): parts.append(part["text"])
        text="".join(parts)
        status=str(result.get("status") or "").strip().lower()
        incomplete=result.get("incomplete_details") if isinstance(result.get("incomplete_details"),dict) else {}
        reason=str(incomplete.get("reason") or "").strip().lower()
        if status=="incomplete" and reason in {"max_output_tokens","max_tokens","length"}: raise Fault("output_truncated")
        if status in {"failed","cancelled"}: raise Fault("provider_incompatible_response",f"response_status:{status}")
        return text,("stop" if status=="completed" else status or None),result.get("usage"),result.get("model"),result.get("id")

    @staticmethod
    def _native_prompt(messages):
        system="\n\n".join(str(x.get("content") or "") for x in messages if x.get("role")=="system").strip() or None
        conversation=[x for x in messages if x.get("role")!="system"]
        if len(conversation)==1 and conversation[0].get("role")=="user": text=str(conversation[0].get("content") or "")
        else: text="\n\n".join(f"[{str(x.get('role') or 'user').upper()}]\n{str(x.get('content') or '')}" for x in conversation)
        return system,text

    @staticmethod
    def _native_result(result):
        parts=[]
        for item in result.get("output",[]):
            if isinstance(item,dict) and item.get("type")=="message" and isinstance(item.get("content"),str): parts.append(item["content"])
        stats=result.get("stats") if isinstance(result.get("stats"),dict) else {}
        usage={}
        for source,target in (("input_tokens","prompt_tokens"),("total_output_tokens","completion_tokens"),("reasoning_output_tokens","reasoning_tokens")):
            value=stats.get(source)
            if isinstance(value,int) and not isinstance(value,bool) and value>=0: usage[target]=value
        if {"prompt_tokens","completion_tokens"}<=set(usage): usage["total_tokens"]=usage["prompt_tokens"]+usage["completion_tokens"]
        return "".join(parts),"stop",usage or None,result.get("model"),result.get("response_id")

    def _lmstudio(self, request, settings):
        base=self.profile["base_url"].rstrip("/")
        reasoning=str(settings.get("reasoning","default")).strip().lower()
        if reasoning not in _LMSTUDIO_REASONING: raise Fault("profile_override_invalid","reasoning")
        if reasoning=="none":
            root=base[:-3] if base.endswith("/v1") else base
            endpoint=root+"/api/v1/chat"
            system,user_input=self._native_prompt(request["messages"])
            data={"model":settings["model"],"input":user_input,"temperature":settings.get("temperature",0),"max_output_tokens":settings.get("max_tokens",4096),"reasoning":"off","stream":False}
            if system is not None: data["system_prompt"]=system
            try: result,request_id=self._post(endpoint,data,request["timeout"])
            except urllib.error.HTTPError as exc: self._raise_http(exc,endpoint)
            parsed=self._native_result(result)
            return parsed,request_id,"api/v1/chat"
        endpoint=base+"/responses"
        data={"model":settings["model"],"input":request["messages"],"temperature":settings.get("temperature",0),"max_output_tokens":settings.get("max_tokens",4096),"stream":False}
        if reasoning!="default": data["reasoning"]={"effort":reasoning}
        try:
            result,request_id=self._post(endpoint,data,request["timeout"])
            parsed=self._responses_result(result)
            return parsed,request_id,"responses"
        except urllib.error.HTTPError as exc:
            if exc.code not in {404,405}: self._raise_http(exc,endpoint)
            # Explicit reasoning must never be silently discarded by a legacy fallback.
            if reasoning!="default":
                detail=self._error_detail(exc,endpoint)
                raise Fault("provider_request_rejected",detail+"; LM Studio /v1/responses is required for explicit reasoning effort") from None
        endpoint=base+"/chat/completions"
        data=self._chat_payload({**settings,"reasoning":"default"},request["messages"])
        try: result,request_id=self._post(endpoint,data,request["timeout"])
        except urllib.error.HTTPError as exc: self._raise_http(exc,endpoint)
        return self._chat_result(result),request_id,"chat/completions"

    def complete(self, request: dict, key: str, index: int) -> dict:
        role=request["role"]
        settings=self.profile["effective_roles"][role]
        executor=self.profile["executor"]
        start=time.monotonic()
        try:
            if executor=="openrouter":
                reasoning=str(settings.get("reasoning","default")).strip().lower()
                if reasoning not in _OPENROUTER_REASONING: raise Fault("profile_override_invalid","reasoning")
                endpoint=self.profile["base_url"].rstrip("/")+"/chat/completions"
                result,request_id=self._post(endpoint,self._chat_payload(settings,request["messages"]),request["timeout"])
                parsed=self._chat_result(result); transport="chat/completions"
            elif executor=="lmstudio":
                parsed,request_id,transport=self._lmstudio(request,settings)
            else: raise Fault("unknown_provider")
            text,finish,usage,returned_model,result_id=parsed
            if not isinstance(text,str) or not text.strip() or len(text.encode("utf-8"))>100_000: raise Fault("provider_incompatible_response")
            return {"text":text,"usage":usage,"returned_model":returned_model,"finish_reason":finish,
                    "provider_request_id":request_id or result_id,"executor":executor,"transport":transport,
                    "reasoning_effort":settings.get("reasoning","default"),"duration":time.monotonic()-start}
        except Fault: raise
        except urllib.error.HTTPError as exc:
            self._raise_http(exc,locals().get("endpoint",self.profile.get("base_url","") or "provider endpoint"))
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

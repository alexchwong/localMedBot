"""Workflow-specific model profiles, memory credentials, and locality classification."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import ipaddress
import os
import secrets
import socket
import urllib.parse
import yaml
from .contracts import Fault

_ALLOWED_PROFILE = {"schema_version","id","name","workflow_id","executor","base_url","model","settings","roles","credential_env"}
_ALLOWED_SETTINGS = {"temperature","max_tokens","timeout_seconds","json_mode"}
_ALLOWED_ROLE = {"model", *_ALLOWED_SETTINGS}
_ROLES = {"extraction","writing","reasoning","match","audit","adjudication","review"}
_EXECUTORS = {"openrouter","lmstudio","self","recorded"}


class CredentialVault:
    def __init__(self):
        self._profile_refs: dict[str,str] = {}
        self._values: dict[str,str] = {}
    def set(self, profile_id: str, value: str | None) -> None:
        if value is None:
            return
        if not isinstance(value, str) or not value:
            raise Fault("credential_invalid")
        self.clear(profile_id)
        ref = secrets.token_urlsafe(32)
        self._profile_refs[profile_id] = ref
        self._values[ref] = value
    def clear(self, profile_id: str) -> None:
        ref = self._profile_refs.pop(profile_id, None)
        if ref:
            self._values.pop(ref, None)
    def get(self, profile_id: str) -> str | None:
        ref = self._profile_refs.get(profile_id)
        return self._values.get(ref) if ref else None
    def known_values(self) -> list[str]:
        return [v for v in self._values.values() if v]


def _merge(a: dict, b: dict) -> dict:
    out = deepcopy(a)
    for k,v in b.items():
        if v is None:
            raise Fault("profile_override_invalid", k)
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def _validate_url(value: str, executor: str) -> str:
    if executor in {"self","recorded"}:
        return value or ""
    p = urllib.parse.urlsplit(value or "")
    if p.scheme not in {"http","https"} or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise Fault("endpoint_invalid")
    return value.rstrip("/")


def validate_profile(doc: dict, runnable: bool = True) -> dict:
    if not isinstance(doc, dict) or set(doc) - _ALLOWED_PROFILE:
        raise Fault("profile_invalid")
    if doc.get("schema_version") != 1 or doc.get("executor") not in _EXECUTORS:
        raise Fault("profile_invalid")
    if not isinstance(doc.get("id"), str) or not isinstance(doc.get("workflow_id"), str):
        raise Fault("profile_invalid")
    if set(doc.get("settings", {})) - _ALLOWED_SETTINGS or set(doc.get("roles", {})) - _ROLES:
        raise Fault("profile_invalid")
    for role, cfg in doc.get("roles", {}).items():
        if not isinstance(cfg, dict) or set(cfg) - _ALLOWED_ROLE:
            raise Fault("profile_invalid", role)
    s = doc.get("settings", {})
    temp = s.get("temperature", 0)
    max_tokens = s.get("max_tokens", 4096)
    timeout = s.get("timeout_seconds", 60)
    if isinstance(temp, bool) or not isinstance(temp,(int,float)) or not 0 <= temp <= 2:
        raise Fault("profile_override_invalid", "temperature")
    if isinstance(max_tokens,bool) or not isinstance(max_tokens,int) or not 1 <= max_tokens <= 32768:
        raise Fault("profile_override_invalid", "max_tokens")
    if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not 1 <= timeout <= 300:
        raise Fault("profile_override_invalid", "timeout_seconds")
    if doc.get("model") is not None and (not isinstance(doc.get("model"),str) or not doc.get("model").strip()):
        raise Fault("profile_override_invalid", "model")
    for role,cfg in doc.get("roles",{}).items():
        if "model" in cfg and (not isinstance(cfg["model"],str) or not cfg["model"].strip()):
            raise Fault("profile_override_invalid", f"roles.{role}.model")
        probe = {**s, **{k:v for k,v in cfg.items() if k in _ALLOWED_SETTINGS}}
        validate_profile({**doc,"settings":probe,"roles":{}}, runnable=False) if cfg else None
    executor = doc["executor"]
    _validate_url(doc.get("base_url", ""), executor)
    if runnable and executor in {"openrouter","lmstudio"} and not doc.get("model"):
        raise Fault("model_missing")
    return doc


_RFC1918 = tuple(ipaddress.ip_network(x) for x in ("10.0.0.0/8","172.16.0.0/12","192.168.0.0/16"))
_ULA = ipaddress.ip_network("fc00::/7")

def _is_local_ip(ip: ipaddress._BaseAddress) -> bool:
    if ip.is_loopback:
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _RFC1918)
    return ip in _ULA


def classify_destination(profile: dict) -> dict:
    executor = profile["executor"]
    if executor == "recorded":
        return {"classification":"local","host":"recorded"}
    if executor == "openrouter":
        return {"classification":"non_local","host":urllib.parse.urlsplit(profile.get("base_url","")).hostname or "openrouter.ai"}
    if executor == "self":
        return {"classification":"unknown","host":"self"}
    host = urllib.parse.urlsplit(profile.get("base_url","")).hostname
    if not host:
        return {"classification":"unknown","host":""}
    if host == "localhost":
        return {"classification":"local_network","host":host}
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            ips = list({ipaddress.ip_address(r[4][0]) for r in socket.getaddrinfo(host,None)})
        except OSError:
            return {"classification":"unknown","host":host}
    return {"classification":"local_network" if ips and all(_is_local_ip(x) for x in ips) else "non_local", "host":host}


class ProfileRegistry:
    def __init__(self, root: str | Path, store, vault: CredentialVault | None = None):
        self.root = Path(root).resolve()
        self.store = store
        self.vault = vault or CredentialVault()

    def templates(self) -> list[dict]:
        rows=[]
        for p in sorted(self.root.glob("*.yaml")):
            doc=yaml.safe_load(p.read_text(encoding="utf-8"))
            validate_profile(doc, runnable=False)
            rows.append(doc)
        return rows

    def template(self, profile_id: str) -> dict:
        rows=[x for x in self.templates() if x["id"]==profile_id]
        if len(rows)!=1:
            raise Fault("profile_not_found")
        return deepcopy(rows[0])

    def list(self, workflow_id: str | None = None) -> list[dict]:
        result=[]
        for t in self.templates():
            if workflow_id and t["workflow_id"] != workflow_id:
                continue
            overlay=self.store.profile_overlay(t["id"])
            effective=self.resolve(t["id"], {}, runnable=False)
            selected=self.store.preference("selected_profile:"+t["workflow_id"])
            result.append({"id":t["id"],"name":t["name"],"workflow_id":t["workflow_id"],"executor":t["executor"],
                           "base_url":effective.get("base_url"),"model":effective.get("model"),"settings":effective.get("settings",{}),"roles":effective.get("roles",{}),
                           "overlay":overlay,"credential_present":bool(self.vault.get(t["id"]) or (effective.get("credential_env") and os.environ.get(effective["credential_env"]))),
                           "destination":classify_destination(effective),"selected":selected==t["id"]})
        return result

    def configure(self, profile_id: str, overlay: dict, credential: str | None = None, clear_credential: bool = False) -> dict:
        if credential is not None and clear_credential:
            raise Fault("credential_invalid")
        t=self.template(profile_id)
        self._validate_overlay(t, overlay)
        self.store.set_profile_overlay(profile_id, overlay)
        if clear_credential:
            self.vault.clear(profile_id)
        elif credential is not None:
            self.vault.set(profile_id, credential)
        return self.resolve(profile_id, {}, runnable=False)

    def _validate_overlay(self, template: dict, overlay: dict) -> None:
        if not isinstance(overlay, dict):
            raise Fault("profile_override_invalid")
        forbidden={"schema_version","id","name","workflow_id","executor","credential_env"}
        if set(overlay)&forbidden or set(overlay)-{"base_url","model","settings","roles"}:
            raise Fault("profile_override_invalid")
        merged=_merge(template,overlay)
        validate_profile(merged,runnable=False)

    def resolve(self, profile_id: str, run_overrides: dict | None = None, workflow_id: str | None = None, runnable: bool = True) -> dict:
        t=self.template(profile_id)
        if workflow_id and t["workflow_id"] != workflow_id:
            raise Fault("profile_workflow_mismatch")
        overlay=self.store.profile_overlay(profile_id)
        self._validate_overlay(t, overlay)
        self._validate_overlay(t, run_overrides or {})
        merged=_merge(_merge(t,overlay),run_overrides or {})
        validate_profile(merged,runnable=runnable)
        base={"model":merged.get("model"), **merged.get("settings",{})}
        effective_roles={}
        for role in _ROLES:
            cfg=merged.get("roles",{}).get(role,{})
            effective_roles[role]=_merge(base,cfg)
        merged["effective_roles"]=effective_roles
        merged["destination"]=classify_destination(merged)
        return merged

    def credential(self, resolved: dict) -> str | None:
        secret=self.vault.get(resolved["id"])
        if secret:
            return secret
        env=resolved.get("credential_env")
        return os.environ.get(env) if env else None

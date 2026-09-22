"""Single aggregation implementation for model-call usage and retry accounting."""
from __future__ import annotations
from collections import defaultdict
from copy import deepcopy


def _number(value):
    return value if isinstance(value,(int,float)) and not isinstance(value,bool) else None


def _usage_fields(call):
    meta=(call or {}).get("response_metadata") or {}
    usage=meta.get("usage") if isinstance(meta.get("usage"),dict) else None
    def pick(source,*names):
        if not isinstance(source,dict): return None
        for name in names:
            value=_number(source.get(name))
            if value is not None: return value
        return None
    if usage is None and not any(_number(meta.get(k)) is not None for k in ("cost","total_cost")):
        token_data=None
    else:
        token_data={
            "input_tokens":pick(usage,"input_tokens","prompt_tokens"),
            "output_tokens":pick(usage,"output_tokens","completion_tokens"),
            "total_tokens":pick(usage,"total_tokens"),
            "cost":pick(usage,"cost","total_cost") if usage else None,
            "currency":usage.get("currency") if isinstance(usage,dict) and isinstance(usage.get("currency"),str) else None,
        }
        if token_data["cost"] is None: token_data["cost"]=pick(meta,"cost","total_cost")
        if token_data["currency"] is None and isinstance(meta.get("currency"),str): token_data["currency"]=meta["currency"]
    duration=pick(meta,"duration","duration_seconds")
    if duration is None and call and call.get("started_at") is not None and call.get("finished_at") is not None:
        try: duration=max(0.0,float(call["finished_at"])-float(call["started_at"]))
        except (TypeError,ValueError): duration=None
    return token_data,duration


def _sum_known(rows,key):
    vals=[r[key] for r in rows if r and r.get(key) is not None]
    return sum(vals) if vals else None


def _summary(calls):
    extracted=[_usage_fields(c) for c in calls]
    usage_rows=[u for u,_ in extracted if u is not None]
    durations=[d for _,d in extracted if d is not None]
    totals={k:_sum_known(usage_rows,k) for k in ("input_tokens","output_tokens","total_tokens","cost")}
    currencies=sorted({u.get("currency") for u in usage_rows if u.get("currency")})
    totals["currency"]=currencies[0] if len(currencies)==1 else ("mixed" if len(currencies)>1 else None)
    totals["duration_seconds"]=sum(durations) if durations else None
    return {
        "reported_calls":len(usage_rows),
        "unreported_calls":len(calls)-len(usage_rows),
        "partial":bool(calls) and len(usage_rows)!=len(calls),
        "duration_reported_calls":len(durations),
        "totals":totals,
    }


def aggregate_usage(requests, physical_calls, semantic_revisions):
    requests=deepcopy(requests or []); calls=deepcopy(physical_calls or []); semantic_revisions=deepcopy(semantic_revisions or [])
    provider_calls=[c for c in calls if c.get("executor") not in {"self","recorded"} and c.get("dispatched")]
    replay_calls=[c for c in calls if c.get("executor")=="recorded" and c.get("dispatched")]
    self_calls=[c for c in calls if c.get("executor")=="self"]
    ops={r.get("operation_id") for r in requests if r.get("operation_id")}
    by_stage={}
    for stage in sorted({r.get("node_id") for r in requests if r.get("node_id")}):
        rqs=[r for r in requests if r.get("node_id")==stage]; ids={r.get("request_id") for r in rqs}; pcs=[c for c in calls if c.get("request_id") in ids]
        actual=[c for c in pcs if c.get("executor") not in {"self","recorded"} and c.get("dispatched")]
        by_stage[stage]={
            "logical_operations":len({r.get("operation_id") for r in rqs if r.get("operation_id")}),
            "requests":len(rqs),
            "physical_calls":len(actual),
            "replay_calls":len([c for c in pcs if c.get("executor")=="recorded" and c.get("dispatched")]),
            "output_repairs":len([r for r in rqs if r.get("purpose")=="output_repair"]),
            "transport_retries":len([c for c in actual if c.get("retry_kind")=="transport"]),
            "self_handoffs":len([c for c in pcs if c.get("executor")=="self"]),
            "usage":_summary(pcs),
        }
    return {
        "logical_operations":len(ops),
        "model_requests":len(requests),
        "physical_calls":len(provider_calls),
        "replay_calls":len(replay_calls),
        "self_handoffs":len(self_calls),
        "output_repairs":len([r for r in requests if r.get("purpose")=="output_repair"]),
        "semantic_revisions":len([r for r in semantic_revisions if r.get("kind")=="automatic"]),
        "transport_retries":len([c for c in provider_calls if c.get("retry_kind")=="transport"]),
        "usage":_summary(calls),
        "by_stage":by_stage,
    }

"""Read-only projections of committed execution history; never drive workflow state."""
from __future__ import annotations


def project_execution(snapshot,developer=False):
    run=snapshot["run"]
    nodes=run.get("snapshot",{}).get("workflow",{}).get("nodes",[])
    attempts=snapshot["step_attempts"]
    calls=snapshot["model_calls"]
    physical=snapshot["physical_calls"]
    repairs=snapshot["repairs"]
    semantic=snapshot["semantic_revisions"]
    reached={a.get("node_id") for a in attempts}
    reached.update(n for n,s in run.get("nodes",{}).items() if s!="pending")
    unavailable={a["id"] for a in snapshot["artifact_history"] if a.get("unavailable") and run.get("active",{}).get(a["id"])==a["revision"]}
    stages=[{"id":n["id"],"model_dependent":True,"state":run.get("nodes",{}).get(n["id"]),"reached":n["id"] in reached,"valid":n["id"] in run.get("active",{}) and n["id"] not in unavailable} for n in nodes if n.get("model_dependent")]
    current=next((s["id"] for s in stages if s["state"] in {"running","retrying","waiting_self","failed","blocked"}),None)
    by_request={c["request_id"]:c for c in calls}
    by_attempt={}
    for a in attempts:
        key=(a["node_id"],a["attempt"])
        if any(s["id"]==key[0] for s in stages):
            by_attempt[key]={"id":f"{key[0]}:{key[1]}","node_id":key[0],"attempt":key[1],"state":a.get("state"),"started":a.get("started"),"completed":a.get("completed"),"requests":[],"repairs":[],"semantic_revisions":[]}
            if developer:
                by_attempt[key]["resolved_inputs"]=a.get("resolved_inputs")
                by_attempt[key]["error"]=a.get("error")
    for c in calls:
        key=(c["node_id"],c["attempt"])
        if key not in by_attempt: continue
        item={"request_id":c["request_id"],"purpose":c.get("purpose"),"status":c.get("status")}
        if developer: item.update(messages=c.get("messages"),raw_response=c.get("raw_response"),parsed_response=c.get("parsed"),audit=c.get("audit"),repair_feedback=c.get("repair_feedback"),effective_model=c.get("effective_model"),response_metadata=c.get("response_metadata"),error=c.get("error"))
        item["calls"]=[{"call_id":p["call_id"],"status":p.get("status"),"executor":p.get("executor"),"retry_kind":p.get("retry_kind"),"transport_ordinal":p.get("transport_ordinal"),"dispatched":p.get("dispatched"),"started_at":p.get("started_at"),"finished_at":p.get("finished_at")} for p in physical if p.get("request_id")==c["request_id"]]
        by_attempt[key]["requests"].append(item)
    for r in repairs:
        key=(r["node_id"],r["attempt"])
        if key in by_attempt:
            next_request=by_request.get(r.get("next_request_id"))
            item={"repair_id":r["repair_id"],"failed_request_id":r.get("failed_call_id"),"next_request_id":r.get("next_request_id"),"ordinal":r.get("repair_ordinal"),"limit":r.get("frozen_limit"),"outcome":r.get("outcome"),"created":r.get("created"),"feedback_issued":bool(next_request and any(p.get("request_id")==next_request["request_id"] and p.get("dispatched") for p in physical))}
            if developer: item.update(findings=r.get("findings"),feedback=r.get("rendered_feedback"))
            by_attempt[key]["repairs"].append(item)
    for s in semantic:
        key=(s.get("checker_id"),s.get("checker_attempt"))
        if key in by_attempt:
            issued=any(c.get("node_id")==s.get("target_id") and c.get("attempt")==s.get("next_target_attempt") and any(p.get("request_id")==c.get("request_id") and p.get("dispatched") for p in physical) for c in calls)
            item={"link_id":s["link_id"],"target_id":s.get("target_id"),"next_target_attempt":s.get("next_target_attempt"),"ordinal":s.get("revision_ordinal"),"limit":s.get("frozen_limit"),"outcome":s.get("outcome"),"created":s.get("created"),"feedback_issued":issued}
            if developer: item.update(findings=s.get("findings"),feedback=s.get("rendered_feedback"))
            by_attempt[key]["semantic_revisions"].append(item)
    # Event IDs are durable order; historical attempts without start events use
    # recorded timestamps and a stable workflow-order tie break.
    start_events={}
    for e in snapshot["events"]:
        if e["kind"]=="node_start":
            data=e.get("data") or {}; start_events.setdefault((data.get("node_id"),data.get("attempt")),e["id"])
    order={n["id"]:i for i,n in enumerate(nodes)}
    timeline=sorted(by_attempt.values(),key=lambda a:(a["started"] if a["started"] is not None else float("inf"),start_events.get((a["node_id"],a["attempt"]),float("inf")),order.get(a["node_id"],9999),a["attempt"]))
    system_failure=run.get("status") in {"failed","blocked"} and (run.get("error") or {}).get("stage") not in {s["id"] for s in stages}
    return {"stages":stages,"reached_count":sum(s["reached"] for s in stages),"total":len(stages),"current_node":current,"system_failure":run.get("error",{}).get("stage") if system_failure and run.get("error") else None,"timeline":timeline,"retry_limits":run.get("retry_limits",{}),"transport_retries":run.get("snapshot",{}).get("policy",{}).get("transport_retries")}
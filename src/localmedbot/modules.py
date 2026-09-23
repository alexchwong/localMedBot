"""Reusable workflow modules for clinical letter and guideline QA."""
from __future__ import annotations
from dataclasses import asdict
from string import Template
from copy import deepcopy
import json
from .contracts import Module,ModuleResult,Registry,CheckResult,Fault
from .knowledge import prepare_sources

CHECK_SCHEMA={"type":"object","required":["status","findings"],"additionalProperties":False,"properties":{
 "status":{"enum":["pass","fail","needs_review"]},
 "findings":{"type":"array","items":{"type":"object","required":["code","severity"],"additionalProperties":False,"properties":{"code":{"type":"string"},"severity":{"enum":["error","info"]},"detail":{"type":"string"},"ids":{"type":"array","items":{"type":"string"}}}}}}}

def ref_key(ref): return (ref["corpus_id"],ref["evidence_id"])
def _uniq_refs(refs):
    seen=set(); out=[]
    for r in refs:
        k=ref_key(r)
        if k not in seen: seen.add(k); out.append({"corpus_id":k[0],"evidence_id":k[1]})
    return out

def _candidate_refs(candidates): return { (x["revision"],x["id"]) for x in candidates }

def _validate_refs(refs,allowed):
    if not refs or any(ref_key(r) not in allowed for r in refs): raise Fault("reference_invalid")


def _finding(code,path,problem,fix,expected=None,received=None,ids=None,kind=None):
    row={"code":code,"instance_location":path,"problem":problem,"required_correction":fix}
    if kind is not None: row["kind"]=kind
    if expected is not None: row["expected"]=expected
    if received is not None: row["received"]=received
    if ids is not None: row["ids"]=ids
    return row

def _reason_contract_validator(inputs):
    def validate_result(value):
        findings=[]
        if not isinstance(value,dict): return findings
        if "facts" in value and isinstance(inputs.get("source"),dict) and "items" in inputs["source"]:
            allowed={(inputs["source"].get("corpus_id"),i.get("id")) for i in inputs["source"].get("items",[])}
            facts=value.get("facts",[]); ids=[f.get("id") for f in facts if isinstance(f,dict)]
            duplicates=sorted({x for x in ids if ids.count(x)>1 and x is not None})
            if duplicates: findings.append(_finding("contract.duplicate_fact_id","/facts","Fact IDs must be unique.","Use one unique ID per extracted fact.",ids=duplicates))
            known=set(ids)
            for i,suggestion in enumerate(value.get("omission_suggestions",[])):
                if suggestion.get("fact_id") not in known:
                    findings.append(_finding("contract.invalid_omission_reference",f"/omission_suggestions/{i}/fact_id","The omission suggestion refers to an unknown fact.","Reference an existing fact ID or remove the unsupported suggestion.",expected=sorted(x for x in known if x),received=suggestion.get("fact_id")))
            for i,fact in enumerate(facts):
                refs=fact.get("evidence_refs",[]) if isinstance(fact,dict) else []
                for j,ref in enumerate(refs):
                    key=(ref.get("corpus_id"),ref.get("evidence_id")) if isinstance(ref,dict) else (None,None)
                    if key not in allowed:
                        findings.append(_finding("contract.invalid_evidence_reference",f"/facts/{i}/evidence_refs/{j}","The fact cites evidence outside the supplied source records.","Use an evidence reference present in the supplied source context.",expected=[list(x) for x in sorted(allowed)],received=list(key)))
        if "claims" in value and isinstance(inputs.get("source"),dict) and "facts" in inputs["source"]:
            fmap={f.get("id"):f for f in inputs["source"].get("facts",[]) if isinstance(f,dict)}; claims=value.get("claims",[]); ids=[c.get("id") for c in claims if isinstance(c,dict)]
            duplicates=sorted({x for x in ids if ids.count(x)>1 and x is not None})
            if duplicates: findings.append(_finding("contract.duplicate_claim_id","/claims","Claim IDs must be unique.","Return at most one claim for each fact ID.",ids=duplicates))
            for i,claim in enumerate(claims):
                cid=claim.get("id") if isinstance(claim,dict) else None
                if cid not in fmap:
                    findings.append(_finding("contract.unknown_fact_reference",f"/claims/{i}/id","The draft claim refers to a fact that was not supplied.","Use only IDs from the supplied facts.",expected=sorted(x for x in fmap if x),received=cid)); continue
                expected={ref_key(r) for r in fmap[cid].get("evidence_refs",[])}; received={ref_key(r) for r in claim.get("evidence_refs",[]) if isinstance(r,dict) and "corpus_id" in r and "evidence_id" in r}
                if received!=expected:
                    findings.append(_finding("contract.source_reference_changed",f"/claims/{i}/evidence_refs","The draft changed the source linkage for this fact.","Preserve the fact's supplied evidence references exactly.",expected=[list(x) for x in sorted(expected)],received=[list(x) for x in sorted(received)]))
        if "document" in value and isinstance(inputs.get("source"),dict) and "facts" in inputs["source"]:
            facts={f["id"]:f for f in inputs["source"]["facts"]}
            for i,row in enumerate(value.get("provenance",[])):
                if row["passage"] not in value["document"]:
                    findings.append(_finding("contract.passage_missing",f"/provenance/{i}/passage","Mapped passage is absent from document.","Map a passage actually present in the drafted document."))
                ids=row["fact_ids"]
                if any(fid not in facts for fid in ids):
                    findings.append(_finding("contract.unknown_fact_reference",f"/provenance/{i}/fact_ids","Unknown extracted fact.","Reference only supplied facts.")); continue
                expected={ref_key(r) for fid in ids for r in facts[fid]["evidence_refs"]}
                if {ref_key(r) for r in row["evidence_refs"]}!=expected:
                    findings.append(_finding("contract.source_reference_changed",f"/provenance/{i}/evidence_refs","Source linkage changed.","Preserve the source references of every mapped fact."))
        return findings
    return validate_result

def _agent_action_validator(context,inputs,retrieved):
    def validate_action(value):
        findings=[]
        if not isinstance(value,dict): return findings
        action=value.get("action")
        if action=="read":
            args=value.get("arguments",{}); cid=args.get("corpus_id"); eid=args.get("evidence_id")
            try: context.knowledge.read(cid,eid)
            except Fault:
                findings.append(_finding("protocol.invented_reference","/arguments","The requested evidence reference does not exist in the frozen run corpus.","Choose a corpus_id/evidence_id available to this run.",received={"corpus_id":cid,"evidence_id":eid},kind="protocol"))
        if action=="submit":
            result=value.get("result",{}); allowed=_candidate_refs(inputs.get("candidates",{}).get("items",[]))|set(retrieved)
            for i,claim in enumerate(result.get("claims",[]) if isinstance(result,dict) else []):
                for j,ref in enumerate(claim.get("evidence_refs",[])):
                    key=ref_key(ref)
                    if key not in allowed: findings.append(_finding("contract.invalid_evidence_reference",f"/result/claims/{i}/evidence_refs/{j}","The claim cites evidence that was not supplied or retrieved.","Use only evidence references available in this operation.",received=list(key)))
        return findings
    return validate_action

def _evidence_stage_validator(claims,candidates):
    allowed=_candidate_refs(candidates); expected={c["id"] for c in claims}
    def validate_result(value):
        findings=[]; rows=value.get("assessments",[]) if isinstance(value,dict) else []; actual=[r.get("claim_id") for r in rows if isinstance(r,dict)]
        if len(actual)!=len(set(actual)):
            findings.append(_finding("contract.duplicate_assessment","/assessments","Each claim may be assessed only once.","Return exactly one assessment per supplied claim."))
        missing=sorted(expected-set(actual)); unknown=sorted(set(actual)-expected)
        if missing: findings.append(_finding("contract.missing_assessment","/assessments","Some supplied claims were not assessed.","Return one assessment for every supplied claim.",ids=missing))
        if unknown: findings.append(_finding("contract.unknown_claim","/assessments","Assessments refer to unknown claim IDs.","Use only supplied claim IDs.",ids=unknown))
        for i,row in enumerate(rows):
            for j,ref in enumerate(row.get("evidence_refs",[]) if isinstance(row,dict) else []):
                key=ref_key(ref)
                if key not in allowed: findings.append(_finding("contract.invalid_evidence_reference",f"/assessments/{i}/evidence_refs/{j}","The assessment cites evidence outside the supplied candidates.","Use only supplied candidate references.",received=list(key)))
            if isinstance(row,dict) and row.get("decision") in {"support","contradiction"} and not row.get("evidence_refs"):
                findings.append(_finding("contract.evidence_required",f"/assessments/{i}/evidence_refs","Support/contradiction decisions require evidence.","Provide at least one supplied evidence reference."))
        return findings
    return validate_result


class Ingestor(Module):
    def execute(self,context,inputs,config):
        profile=config["profile"]; data=inputs["data"]; sources=data["sources"]
        def extract(chunk):
            schema={"type":"object","required":["items"],"additionalProperties":False,"properties":{"items":{"type":"array","items":profile["item_schema"]}}}
            return context.call(profile["prompt"],{"chunk":chunk,"indexes":profile["indexes"]},schema,"extraction")["items"]
        items=prepare_sources(sources,profile,extract); scope=context.run["id"] if config.get("scope","run")=="run" else context.run["snapshot"]["manifest"]["id"]
        cid=context.store.publish(scope,config.get("name","records"),profile,sources,items)
        if cid not in context.run["corpora"]: context.run["corpora"].append(cid)
        if context.run["snapshot"]["manifest"]["id"]=="clinical_letter":
            origin=context.run.get("origins",{}).get("task_input","unknown")
            evidence=context.run.setdefault("origins",{}).setdefault("evidence",{})
            evidence.pop("clinical_input",None); evidence[cid]=origin
            vals=[context.run["origins"].get("task_input"),*evidence.values(),*context.run["origins"].get("revision_feedback",{}).values()]
            context.run["data_origin"]="unknown" if not vals or "unknown" in vals else "mixed" if len(set(vals))>1 else vals[0]
        return ModuleResult({"corpus_id":cid,"items":[{**x,"revision":cid} for x in items]})


class Retriever(Module):
    def execute(self,context,inputs,config):
        if config.get("mode")=="collect":
            items={}
            for c in inputs.get("initial",{}).get("items",[])+inputs.get("discovered",[]):
                item=context.knowledge.read(c["revision"],c["id"]); item.pop("origin",None); items[(item["revision"],item["id"])]=item
            if len(items)>config.get("limit",48): raise Fault("candidate_limit")
            return ModuleResult({"items":list(items.values())})
        data=inputs.get("data",{}); query=data.get(config.get("query_field","question"),"")
        return ModuleResult({"items":context.knowledge.search(query,data.get("filters",[]),config.get("limit",12))})


def _partition(alt):
    return tuple(sorted(set(ref_key(x) for x in alt.get("evidence_refs",[]))))

def _partition_key(part):
    return json.dumps(list(part),ensure_ascii=False,separators=(",",":"))

def _conflict_fingerprint(report):
    partitions=[_partition(alt) for alt in report.get("alternatives",[])]
    if len(partitions)<2 or any(not x for x in partitions) or len(set(partitions))!=len(partitions): return None
    return json.dumps([list(x) for x in sorted(partitions)],ensure_ascii=False,separators=(",",":"))

def _complete_relations(report,claims):
    rows=report.get("claim_relations",[]); ids=[r.get("claim_id") for r in rows]; expected=[c["id"] for c in claims]
    if len(ids)!=len(set(ids)) or set(ids)!=set(expected): raise Fault("conflict_claim_mapping_invalid")
    if any(r.get("relation") not in {"implicated","unrelated","uncertain"} for r in rows): raise Fault("conflict_claim_mapping_invalid")

def _registry_fingerprint(conflict):
    report={"alternatives":conflict["alternatives"]}; return _conflict_fingerprint(report)

def _store_fp(conflict,fp):
    if not fp: return
    mapping={_partition_key(_partition(a)):a["id"] for a in conflict["alternatives"]}
    if fp not in conflict.setdefault("fingerprints",[]): conflict["fingerprints"].append(fp)
    conflict.setdefault("fingerprint_maps",{})[fp]=mapping

def update_conflict_registry(registry,reports,claims,allowed_refs,origin):
    reg=deepcopy(registry or {"conflicts":[],"next_conflict":1})
    for report in reports:
        _complete_relations(report,claims)
        for alt in report.get("alternatives",[]):
            _validate_refs(alt.get("evidence_refs",[]),allowed_refs)
            bad=set(alt.get("claim_ids",[]))-{c["id"] for c in claims}
            if bad: raise Fault("conflict_claim_mapping_invalid")
            implicated={r["claim_id"] for r in report["claim_relations"] if r["relation"]=="implicated"}
            if not set(alt.get("claim_ids",[]))<=implicated: raise Fault("conflict_claim_mapping_invalid")
        extends=report.get("extends_conflict_id")
        if extends:
            target=next((x for x in reg["conflicts"] if x["id"]==extends),None)
            if not target: raise Fault("conflict_extension_invalid")
            mapping={a.get("extends_alternative_id"):a for a in report["alternatives"] if a.get("extends_alternative_id")}
            existing={a["id"] for a in target["alternatives"]}
            if set(mapping)!=existing: raise Fault("conflict_extension_invalid")
            for old in target["alternatives"]:
                incoming=mapping[old["id"]]
                if not set(map(ref_key,old["evidence_refs"]))<=set(map(ref_key,incoming["evidence_refs"])): raise Fault("conflict_extension_invalid")
                old["evidence_refs"]=_uniq_refs(old["evidence_refs"]+incoming["evidence_refs"]); old["claim_ids"]=sorted(set(old["claim_ids"]+incoming.get("claim_ids",[])))
                if incoming.get("statement") not in old["statement_variants"]: old["statement_variants"].append(incoming.get("statement",""))
                if incoming.get("applicability") not in old["applicability_variants"]: old["applicability_variants"].append(incoming.get("applicability",""))
            for incoming in [a for a in report["alternatives"] if not a.get("extends_alternative_id")]:
                aid=f"{target['id']}.alt-{len(target['alternatives'])+1:04d}"
                target["alternatives"].append({"id":aid,"statement":incoming["statement"],"evidence_refs":_uniq_refs(incoming["evidence_refs"]),"applicability":incoming.get("applicability",""),"claim_ids":list(incoming.get("claim_ids",[])),"statement_variants":[incoming["statement"]],"applicability_variants":[incoming.get("applicability","")]})
            target["report_origins"].append(origin); target["claim_relations"].extend([{"origin":origin,**r} for r in report["claim_relations"]]); _store_fp(target,_registry_fingerprint(target)); continue
        fp=_conflict_fingerprint(report); matches=[x for x in reg["conflicts"] if fp and fp in x.get("fingerprints",[])]
        if len(matches)>1: raise Fault("conflict_identity_ambiguous")
        merge=matches[0] if matches else None
        if merge:
            pmap=merge.get("fingerprint_maps",{}).get(fp,{})
            byid={a["id"]:a for a in merge["alternatives"]}
            for alt in report["alternatives"]:
                old=byid.get(pmap.get(_partition_key(_partition(alt))))
                if old:
                    old["claim_ids"]=sorted(set(old["claim_ids"]+alt.get("claim_ids",[])))
                    if alt["statement"] not in old["statement_variants"]: old["statement_variants"].append(alt["statement"])
                    if alt.get("applicability","") not in old["applicability_variants"]: old["applicability_variants"].append(alt.get("applicability",""))
            merge["claim_relations"].extend([{"origin":origin,**r} for r in report["claim_relations"]]); merge["report_origins"].append(origin)
        else:
            cid=f"conflict-{reg['next_conflict']:04d}"; reg["next_conflict"]+=1; alternatives=[]
            for idx,alt in enumerate(report.get("alternatives",[]),1):
                alternatives.append({"id":f"{cid}.alt-{idx:04d}","statement":alt["statement"],"evidence_refs":_uniq_refs(alt["evidence_refs"]),"applicability":alt.get("applicability",""),"claim_ids":list(alt.get("claim_ids",[])),"statement_variants":[alt["statement"]],"applicability_variants":[alt.get("applicability","")]})
            conflict={"id":cid,"topic":report.get("topic",""),"alternatives":alternatives,"assessment_note":report.get("assessment_note",""),"claim_relations":[{"origin":origin,**r} for r in report["claim_relations"]],"report_origins":[origin],"fingerprints":[],"fingerprint_maps":{}}
            _store_fp(conflict,fp); reg["conflicts"].append(conflict)
    return reg


class ReasoningHead(Module):
    model_dependent=True
    def execute(self,context,inputs,config):
        schema=context.node.get("schema",{})
        if config.get("mode","single")=="single": return ModuleResult(context.call(config["prompt"],inputs,schema,config.get("role","reasoning"),validators=[_reason_contract_validator(inputs)]))
        # model submission excludes runtime-owned retrieved/conflict_registry fields
        submission=deepcopy(schema); props=submission.get("properties",{}); props.pop("retrieved",None); props.pop("conflict_registry",None); submission["required"]=[x for x in submission.get("required",[]) if x not in {"retrieved","conflict_registry"}]
        action_schema={"oneOf":[
          {"type":"object","required":["action","arguments"],"additionalProperties":False,"properties":{"action":{"const":"search"},"arguments":{"type":"object","required":["query"],"additionalProperties":False,"properties":{"query":{"type":"string"},"filters":{"type":"array"},"limit":{"type":"integer","minimum":1,"maximum":12},"mode":{"const":"lexical"}}}}},
          {"type":"object","required":["action","arguments"],"additionalProperties":False,"properties":{"action":{"const":"read"},"arguments":{"type":"object","required":["corpus_id","evidence_id"],"additionalProperties":False,"properties":{"corpus_id":{"type":"string"},"evidence_id":{"type":"string"}}}}},
          {"type":"object","required":["action","result"],"additionalProperties":False,"properties":{"action":{"const":"submit"},"result":submission}}
        ]}
        messages={"inputs":inputs,"observations":[],"tools":config.get("tools",[])}; retrieved={}
        for _ in range(config.get("max_turns",6)):
            allowed_actions=[
              {"name":"search","arguments_schema":action_schema["oneOf"][0]["properties"]["arguments"]},
              {"name":"read","arguments_schema":action_schema["oneOf"][1]["properties"]["arguments"]},
              {"name":"submit","result_schema":submission}
            ]
            action=context.call(config["prompt"],messages,action_schema,config.get("role","reasoning"),allowed_actions=allowed_actions,validators=[_agent_action_validator(context,inputs,retrieved)])
            if action["action"]=="submit":
                result=action["result"]; claims=result.get("claims",[]); allowed=_candidate_refs(inputs.get("candidates",{}).get("items",[]))|set(retrieved)
                for c in claims:
                    if c.get("evidence_refs") and not set(map(ref_key,c["evidence_refs"]))<=allowed: raise Fault("reference_invalid")
                result["retrieved"]=list(retrieved.values())
                if "conflicts" in result:
                    result["conflict_registry"]=update_conflict_registry(None,result.get("conflicts",[]),claims,allowed,"reason")
                return ModuleResult(result)
            name={"search":"evidence.search","read":"evidence.read"}[action["action"]]; args=action["arguments"]
            if name=="evidence.search": args.setdefault("limit",12); args.setdefault("mode","lexical"); args.setdefault("filters",[])
            obs=context.tool(name,args)
            for item in obs if isinstance(obs,list) else [obs]: retrieved[(item["revision"],item["id"])]={k:v for k,v in item.items() if k!="origin"}
            if len(retrieved)>48: raise Fault("candidate_limit")
            messages["observations"].append({"action":action,"result":obs})
        raise Fault("agent_turn_limit")


class ContentCheck(Module):
    model_dependent=True
    def execute(self,context,inputs,config):
        findings=[]; target=inputs["target"]
        if config.get("references"):
            allowed={(inputs["source"]["corpus_id"],i["id"]) for i in inputs["source"]["items"]}
            facts=target.get("facts",[])
            ids=[f.get("id") for f in facts]
            if len(ids)!=len(set(ids)): raise Fault("reference_invalid","duplicate_fact")
            known=set(ids)
            for suggestion in target.get("omission_suggestions",[]):
                if suggestion.get("fact_id") not in known: raise Fault("reference_invalid","omission_suggestion")
            if inputs.get("task",{}).get("sources") and not facts: raise Fault("no_extractable_facts")
            for f in facts:
                refs=f.get("evidence_refs",[])
                if not refs or not set(map(ref_key,refs))<=allowed: raise Fault("reference_invalid","unknown_source")
        if config.get("coverage"):
            facts=inputs["source"]["facts"]; fmap={f["id"]:f for f in facts}; policy=inputs.get("omission_policy",{}); omit=set(policy.get("omitted_fact_ids",[]));
            if omit and (policy.get("extraction_revision")!=context.run["active"].get("facts") or not omit<=set(fmap) or set(policy.get("reasons",{}))!=omit): raise Fault("reference_invalid","stale_omission_policy")
            expected=set(fmap)-omit; claims=target.get("claims",[]); ids=[c["id"] for c in claims]
            if len(ids)!=len(set(ids)): raise Fault("reference_invalid","duplicate_claim")
            actual=set(ids)
            if expected-actual: findings.append({"code":"omitted_fact","severity":"error","ids":sorted(expected-actual)})
            if actual & omit: findings.append({"code":"forbidden_omitted_fact_included","severity":"error","ids":sorted(actual & omit)})
            if actual-set(fmap): raise Fault("reference_invalid","unknown_fact")
            for c in claims:
                if c["id"] in fmap and set(map(ref_key,c.get("evidence_refs",[])))!=set(map(ref_key,fmap[c["id"]].get("evidence_refs",[]))): raise Fault("reference_invalid","source_changed")
        if config.get("document_coverage"):
            facts=inputs["source"]["facts"]; fmap={f["id"]:f for f in facts}; policy=inputs.get("omission_policy",{}); omit=set(policy.get("omitted_fact_ids",[]))
            if omit and (policy.get("extraction_revision")!=context.run["active"].get("facts") or not omit<=set(fmap) or set(policy.get("reasons",{}))!=omit): raise Fault("reference_invalid","stale_omission_policy")
            mapped=set()
            for row in target["provenance"]:
                if row["passage"] not in target["document"]: raise Fault("reference_invalid","passage_missing")
                ids=set(row["fact_ids"])
                if not ids<=set(fmap): raise Fault("reference_invalid","unknown_fact")
                expected={ref_key(ref) for fid in ids for ref in fmap[fid]["evidence_refs"]}
                if {ref_key(ref) for ref in row["evidence_refs"]}!=expected: raise Fault("reference_invalid","source_changed")
                mapped.update(ids)
            if set(fmap)-omit-mapped: findings.append({"code":"omitted_fact","severity":"error","ids":sorted(set(fmap)-omit-mapped)})
            if mapped & omit: findings.append({"code":"forbidden_omitted_fact_included","severity":"error","ids":sorted(mapped & omit)})
        if config.get("accepted_only"):
            source=inputs["source"]; accepted={c["id"] for c in source.get("accepted",[])}; rendered=set(target.get("claim_ids",[])); withheld=set(source.get("withheld_claim_ids",[]))
            if rendered!=accepted or rendered&withheld: raise Fault("reference_invalid","unaccepted_rendered_claim")
            if set(target.get("conflict_ids",[]))!={c["id"] for c in source.get("conflicts",[])}: raise Fault("reference_invalid","conflict_not_rendered")
        review=context.call(config["prompt"],inputs,CHECK_SCHEMA,"review")
        findings.extend(review["findings"]); any_error=any(f.get("severity","error")=="error" for f in findings)
        status="fail" if any_error or review["status"] in {"fail","needs_review"} else "pass"
        return ModuleResult(asdict(CheckResult(status,findings)))


def assessment_schema(conflict_schema):
    return {"type":"object","required":["assessments","conflicts"],"additionalProperties":False,"properties":{
      "assessments":{"type":"array","items":{"type":"object","required":["claim_id","evidence_refs","decision","reason"],"additionalProperties":False,"properties":{"claim_id":{"type":"string"},"evidence_refs":{"type":"array","items":{"type":"object","required":["corpus_id","evidence_id"],"additionalProperties":False,"properties":{"corpus_id":{"type":"string"},"evidence_id":{"type":"string"}}},"uniqueItems":True},"decision":{"enum":["support","contradiction","insufficient","not_applicable"]},"reason":{"type":"string"}}}},
      "conflicts":{"type":"array","items":conflict_schema}}}


class EvidenceStage(Module):
    model_dependent=True
    def execute(self,context,inputs,config):
        claims=inputs["claims"]["claims"]; candidates=inputs["candidates"]["items"]; allowed=_candidate_refs(candidates); conflict_schema=config["conflict_schema"]
        result=context.call(config["prompt"],inputs,assessment_schema(conflict_schema),config.get("role","review"),validators=[_evidence_stage_validator(claims,candidates)]); rows=result["assessments"]
        expected={c["id"] for c in claims}; actual=[r["claim_id"] for r in rows]
        if len(actual)!=len(set(actual)) or set(actual)!=expected: raise Fault("reference_invalid")
        for row in rows:
            refs=row["evidence_refs"]
            if not set(map(ref_key,refs))<=allowed or (row["decision"] in {"support","contradiction"} and not refs): raise Fault("reference_invalid")
        previous=inputs.get("previous"); disputes=False
        if previous:
            before={r["claim_id"]:r for r in previous["assessments"]}; disputes=any(r["decision"]!=before[r["claim_id"]]["decision"] or set(map(ref_key,r["evidence_refs"]))!=set(map(ref_key,before[r["claim_id"]]["evidence_refs"])) for r in rows)
        registry=update_conflict_registry(inputs.get("conflict_registry") or inputs["claims"].get("conflict_registry"),result.get("conflicts",[]),claims,allowed,context.node["id"])
        if result.get("conflicts"): disputes=True
        return ModuleResult({"assessments":rows,"conflicts":result.get("conflicts",[]),"conflict_registry":registry,"disputed":disputes})


class EvidenceFinalize(Module):
    def execute(self,context,inputs,config):
        claims=inputs["claims"]["claims"]; candidates={(x["revision"],x["id"]):x for x in inputs["candidates"]["items"]}; final=inputs.get("adjudication") or inputs["audit"]; registry=final.get("conflict_registry") or inputs["audit"].get("conflict_registry") or {"conflicts":[]}
        assessments={r["claim_id"]:r for r in final["assessments"]}; withheld=set()
        for conflict in registry.get("conflicts",[]):
            for rel in conflict.get("claim_relations",[]):
                if rel.get("relation") in {"implicated","uncertain"}: withheld.add(rel["claim_id"])
            alt_refs=set()
            for alt in conflict["alternatives"]:
                withheld.update(alt.get("claim_ids",[])); alt_refs|=set(map(ref_key,alt["evidence_refs"]))
            for claim in claims:
                row=assessments.get(claim["id"],{}); claim_refs=set(map(ref_key,claim.get("evidence_refs",[])))|set(map(ref_key,row.get("evidence_refs",[])))
                if claim_refs&alt_refs: withheld.add(claim["id"])
        accepted=[]; unresolved=[]
        for claim in claims:
            row=assessments[claim["id"]]
            if claim["id"] in withheld: unresolved.append({"claim_id":claim["id"],"decision":"conflicting","reason":"claim_conflict_linked"}); continue
            if row["decision"]=="support" and row["evidence_refs"]:
                accepted.append({**claim,"evidence_refs":row["evidence_refs"],"support":[{"id":r["evidence_id"],"revision":r["corpus_id"]} for r in row["evidence_refs"]]})
            else: unresolved.append({"claim_id":claim["id"],"decision":row["decision"],"reason":row["reason"]})
        conflicts=registry.get("conflicts",[]); outcome="conflicting" if conflicts else "partial" if accepted and unresolved else "supported" if accepted else "no_evidence"
        return ModuleResult({"accepted":accepted,"conflicts":conflicts,"unresolved":unresolved,"withheld_claim_ids":sorted(withheld),"dissent":{"match":inputs["match"],"audit":inputs["audit"],"adjudication":inputs.get("adjudication")},"evidence_outcome":outcome})


class Renderer(Module):
    def execute(self,context,inputs,config):
        data=inputs["data"]; citations=[]; sections=[]; claim_ids=[]; conflict_ids=[]
        if config.get("document"):
            return ModuleResult({"text":data["document"],"sections":[],"claim_ids":sorted({fid for row in data["provenance"] for fid in row["fact_ids"]}),"citations":[],"conflict_ids":[],"unresolved":[],"provenance":data["provenance"],"data_origin":context.run.get("data_origin","unknown"),"origins":context.run.get("origins",{}),"executor":context.run["profile"]["executor"],"evidence_outcome":None})
        def cite(refs):
            nums=[]
            for ref in refs:
                origin=context.knowledge.read(ref["corpus_id"],ref["evidence_id"]); c={"evidence_id":ref["evidence_id"],"corpus_id":ref["corpus_id"],"source":origin["source"],"locator":origin["locator"]}
                if c not in citations: citations.append(c)
                nums.append(str(citations.index(c)+1))
            return nums
        claims=data.get("accepted",data.get("claims",[]))
        for claim in claims:
            nums=cite(claim.get("evidence_refs",[])) if config.get("user_citations",True) else []; sections.append({"kind":"claim","id":claim["id"],"text":claim["text"],"citations":nums}); claim_ids.append(claim["id"])
        for conflict in data.get("conflicts",[]):
            conflict_ids.append(conflict["id"]); alts=[]
            for alt in conflict["alternatives"]:
                nums=cite(alt["evidence_refs"]) if config.get("user_citations",True) else []; alts.append({"id":alt["id"],"text":alt["statement"],"applicability":alt.get("applicability",""),"citations":nums})
            sections.append({"kind":"conflict","id":conflict["id"],"topic":conflict.get("topic",""),"alternatives":alts})
        for item in data.get("unresolved",[]): sections.append({"kind":"unresolved","id":item.get("claim_id"),"text":item.get("reason","")})
        lines=[]
        for s in sections:
            if s["kind"]=="claim": lines.append(s["text"]+(" ["+", ".join(s["citations"])+"]" if s["citations"] else ""))
            elif s["kind"]=="conflict":
                lines.append("Conflicting guidance — "+s["topic"])
                for alt in s["alternatives"]: lines.append("Alternative: "+alt["text"]+(" ["+", ".join(alt["citations"])+"]" if alt["citations"] else ""))
            else: lines.append("Unresolved: "+s["text"])
        if not lines: lines=["No supported answer is available from the supplied sources."]
        title=data.get("title",config.get("title","Draft")); text=Template(config["template"]).substitute(body="\n\n".join(lines),title=title)
        return ModuleResult({"text":text,"sections":sections,"claim_ids":claim_ids,"citations":citations,"conflict_ids":conflict_ids,"unresolved":data.get("unresolved",[]),"data_origin":context.run.get("data_origin","unknown"),"origins":context.run.get("origins",{}),"executor":context.run["profile"]["executor"],"evidence_outcome":data.get("evidence_outcome")})


class HumanReview(Module):
    def execute(self,context,inputs,config): return ModuleResult({"requested":True},"waiting_review")


def registry():
    r=Registry()
    for name,module in [("ingest",Ingestor()),("retrieve",Retriever()),("reason",ReasoningHead()),("content_check",ContentCheck()),("evidence_stage",EvidenceStage()),("evidence_finalize",EvidenceFinalize()),("render",Renderer()),("human_review",HumanReview())]: r.register(name,module)
    return r

"""Reusable module implementations and evidence-chain composition."""
from dataclasses import asdict
import json
from string import Template
from .contracts import Module, ModuleResult, Registry, CheckResult, Fault, parse, validate
from .knowledge import prepare_sources, Knowledge


CHECK_SCHEMA = {"type":"object","required":["status","findings"],"additionalProperties":False,
                "properties":{"status":{"enum":["pass","fail","needs_review"]},"findings":{"type":"array","items":{"type":"object","required":["code"],"properties":{"code":{"type":"string"},"detail":{"type":"string"}},"additionalProperties":True}}}}


class SyntaxCheck(Module):
    def execute(self, context, inputs, config):
        return ModuleResult(parse(inputs["raw"]))


class SchemaCheck(Module):
    def execute(self, context, inputs, config):
        return ModuleResult(validate(inputs["value"], config["schema"]))


def checked(raw, schema):
    value = SyntaxCheck().execute(None,{"raw":raw},{}).payload
    return SchemaCheck().execute(None,{"value":value},{"schema":schema}).payload


class Ingestor(Module):
    def execute(self, context, inputs, config):
        profile = config["profile"]
        data = inputs["data"]
        sources = data["sources"]
        def extract(chunk):
            schema = {"type":"object","required":["items"],"properties":{"items":{"type":"array","items":profile["item_schema"]}},"additionalProperties":False}
            raw = context.call(profile["prompt"], {"chunk":chunk,"indexes":profile["indexes"]},schema,role="extraction")
            return checked(raw,schema)["items"]
        items = prepare_sources(sources,profile,extract)
        scope = context.run["id"] if config.get("scope", "run") == "run" else context.run["snapshot"]["manifest"]["id"]
        cid = context.store.publish(scope,config.get("name","records"),profile,sources,items)
        if cid not in context.run["corpora"]:
            context.run["corpora"].append(cid)
        return ModuleResult({"corpus_id":cid,"items":[{**x,"revision":cid} for x in items]})


class Retriever(Module):
    def execute(self, context, inputs, config):
        data = inputs.get("data", {})
        if config.get("mode") == "collect":
            items = {}
            for candidate in inputs.get("initial", {}).get("items", []) + inputs.get("discovered", []):
                item = context.knowledge.read(candidate["revision"], candidate["id"])
                item.pop("origin", None)
                key = item["id"]
                if key in items and items[key]["revision"] != item["revision"]:
                    raise Fault("ambiguous_evidence_id", key)
                items[key] = item
            if len(items) > config.get("limit", 12):
                raise Fault("candidate_limit")
            return ModuleResult({"items": list(items.values())})
        query = data.get(config.get("query_field", "question"), "")
        result = context.knowledge.search(query, data.get("filters", []), config.get("limit",12))
        return ModuleResult({"items":result})


class ReasoningHead(Module):
    def execute(self, context, inputs, config):
        schema = context.node.get("schema", {})
        if config.get("mode", "single") == "single":
            raw = context.call(config["prompt"], inputs, schema,config.get("role","reasoning"))
            return ModuleResult(checked(raw,schema))
        submission_schema = json.loads(json.dumps(schema))
        submission_schema.get("properties", {}).pop("retrieved", None)
        retrieved = {}
        action_schema = {"type":"object","required":["action"],"properties":{
            "action":{"enum":["search","read","submit"]},"arguments":{"type":"object"},"result":submission_schema},"additionalProperties":False}
        messages = {"inputs":inputs,"observations":[],"tools":config.get("tools",[])}
        for _ in range(config.get("max_turns",6)):
            action = checked(context.call(config["prompt"],messages,action_schema,config.get("role","reasoning")),action_schema)
            if action["action"] == "submit":
                if "result" not in action:
                    raise Fault("schema_invalid")
                if "retrieved" in schema.get("properties", {}):
                    action["result"]["retrieved"] = list(retrieved.values())
                validate(action["result"],schema)
                return ModuleResult(action["result"])
            name = {"search":"evidence.search","read":"evidence.read"}[action["action"]]
            result = context.tool(name,action.get("arguments",{}))
            for item in result if isinstance(result, list) else [result]:
                retrieved[(item["revision"], item["id"])] = {k:v for k,v in item.items() if k != "origin"}
            messages["observations"].append({"action":action,"result":result})
        raise Fault("agent_turn_limit")


class ContentCheck(Module):
    def execute(self, context, inputs, config):
        findings = []
        target = inputs["target"]
        if config.get("coverage"):
            source = inputs["source"]
            facts = source[config.get("source_list","facts")]
            claims = target.get("claims", [])
            required = {f["id"] for f in facts if f.get("required",True)}
            actual = {c["id"] for c in claims}
            if not required <= actual:
                findings.append({"code":"omitted_fact","ids":sorted(required-actual)})
            known = {f["id"] for f in facts}
            if actual-known:
                findings.append({"code":"unknown_fact","ids":sorted(actual-known)})
            fact_map = {f["id"]:f for f in facts}
            for claim in claims:
                fact = fact_map.get(claim["id"])
                if fact and set(claim.get("evidence_ids",[])) != set(fact.get("evidence_ids",[])):
                    findings.append({"code":"source_changed","id":claim["id"]})
        if config.get("references"):
            required_ids = set(inputs.get("task",{}).get("required_fact_ids",[]))
            actual_ids = {f["id"] for f in target.get(config.get("target_list","facts"),[])}
            if not required_ids <= actual_ids:
                findings.append({"code":"omitted_fact","ids":sorted(required_ids-actual_ids)})
            allowed = {i["id"] for i in inputs["source"]["items"]}
            for fact in target.get(config.get("target_list","facts"),[]):
                if not fact.get("evidence_ids") or not set(fact["evidence_ids"]) <= allowed:
                    findings.append({"code":"unknown_source","id":fact["id"]})
        if config.get("accepted_only"):
            allowed = {c["id"] for c in inputs["source"]["accepted"]}
            if set(target.get("claim_ids",[])) != allowed:
                findings.append({"code":"unaccepted_rendered_claim"})
        # Independent fresh invocation, without the writer's conversational state.
        review = checked(context.call(config["prompt"],inputs,CHECK_SCHEMA,role="review"),CHECK_SCHEMA)
        findings.extend(review["findings"])
        status = "fail" if findings else review["status"]
        return ModuleResult(asdict(CheckResult(status,findings)))


def assessment_schema():
    return {"type":"object","required":["assessments"],"additionalProperties":False,"properties":{
        "assessments":{"type":"array","items":{"type":"object","required":["claim_id","evidence_ids","decision","reason"],"additionalProperties":False,"properties":{
            "claim_id":{"type":"string"},"evidence_ids":{"type":"array","items":{"type":"string"},"uniqueItems":True},
            "decision":{"enum":["support","contradiction","insufficient"]},"reason":{"type":"string","minLength":1}}}}}}


class EvidenceStage(Module):
    def execute(self, context, inputs, config):
        claims = inputs["claims"]["claims"]
        candidates = inputs["candidates"]["items"]
        schema = assessment_schema()
        rows = checked(context.call(config["prompt"],inputs,schema,role=config.get("role","review")),schema)["assessments"]
        claim_ids, evidence_ids = {c["id"] for c in claims}, {e["id"] for e in candidates}
        if len(claim_ids) != len(claims) or any(not set(c.get("evidence_ids",[])) <= evidence_ids for c in claims):
            raise Fault("reference_invalid", "claim envelope")
        actual = [r["claim_id"] for r in rows]
        if len(actual) != len(set(actual)) or set(actual) != claim_ids:
            raise Fault("reference_invalid", "assessment coverage")
        for row in rows:
            if not set(row["evidence_ids"]) <= evidence_ids or (row["decision"] in ["support","contradiction"] and not row["evidence_ids"]):
                raise Fault("reference_invalid", "evidence envelope")
            if config.get("selected_only"):
                matched = next(r for r in inputs["previous"]["assessments"] if r["claim_id"] == row["claim_id"])
                if not set(row["evidence_ids"]) <= set(matched["evidence_ids"]):
                    raise Fault("reference_invalid", "audit envelope")
        disputes = False
        if "previous" in inputs:
            before = {r["claim_id"]:r for r in inputs["previous"]["assessments"]}
            disputes = any(r["decision"] != before[r["claim_id"]]["decision"] or set(r["evidence_ids"]) != set(before[r["claim_id"]]["evidence_ids"]) for r in rows)
        return ModuleResult({"assessments":rows,"disputed":disputes})


class EvidenceFinalize(Module):
    def execute(self, context, inputs, config):
        claims = inputs["claims"]["claims"]
        candidates = {e["id"]:e for e in inputs["candidates"]["items"]}
        final = inputs.get("adjudication") or inputs["audit"]
        assessments = {r["claim_id"]:r for r in final["assessments"]}
        accepted, unresolved = [], []
        for claim in claims:
            row = assessments[claim["id"]]
            if row["decision"] == "support" and row["evidence_ids"]:
                accepted.append({**claim,"evidence_ids":row["evidence_ids"],"support":[{"id":eid,"revision":candidates[eid]["revision"]} for eid in row["evidence_ids"]]})
            else:
                unresolved.append({"claim_id":claim["id"],"decision":row["decision"],"reason":row["reason"]})
        return ModuleResult({"accepted":accepted,"unresolved":unresolved,"dissent":{"match":inputs["match"],"audit":inputs["audit"],"adjudication":inputs.get("adjudication")}})


class EvidenceChain:
    """Expands a reusable component into independently persisted runtime nodes."""
    @staticmethod
    def nodes(prefix, claims, candidates, prompts):
        base = {"claims":f"artifacts.{claims}","candidates":f"artifacts.{candidates}"}
        match, audit, adjudicate, final = [prefix+"."+x for x in ["match","audit","adjudicate","final"]]
        return [
            {"id":match,"module":"evidence_stage","needs":[claims,candidates],"inputs":base,"config":{"prompt":prompts["match"],"role":"match"},"repairs":1},
            {"id":audit,"module":"evidence_stage","needs":[match],"inputs":{**base,"previous":f"artifacts.{match}"},"config":{"prompt":prompts["audit"],"selected_only":True},"repairs":1},
            {"id":adjudicate,"module":"evidence_stage","needs":[audit],"inputs":{**base,"previous":f"artifacts.{audit}","match":f"artifacts.{match}"},"when":{"from":f"artifacts.{audit}.disputed","op":"equals","value":True},"config":{"prompt":prompts["adjudicate"]},"repairs":1},
            {"id":final,"module":"evidence_finalize","needs":[adjudicate],"inputs":{**base,"match":f"artifacts.{match}","audit":f"artifacts.{audit}","adjudication":{"from":f"artifacts.{adjudicate}","optional":True}}}]


class Renderer(Module):
    def execute(self, context, inputs, config):
        data = inputs["data"]
        claims = data.get("accepted",data.get("claims",[]))
        citations, lines = [], []
        for claim in claims:
            numbers = []
            supports = claim.get("support")
            if supports is None:
                supports = []
                for eid in claim.get("evidence_ids",[]):
                    found = None
                    for cid in context.run["corpora"]:
                        try:
                            context.knowledge.read(cid,eid); found = {"id":eid,"revision":cid}; break
                        except Fault:
                            continue
                    if not found:
                        raise Fault("reference_invalid")
                    supports.append(found)
            for support in supports:
                origin = context.knowledge.read(support["revision"],support["id"])
                citation = {"evidence_id":support["id"],"corpus_id":support["revision"],"source":origin["source"],"locator":origin["locator"]}
                if citation not in citations:
                    citations.append(citation)
                numbers.append(str(citations.index(citation)+1))
            lines.append(claim["text"] + (" ["+", ".join(numbers)+"]" if numbers else ""))
        unresolved = data.get("unresolved",[])
        if unresolved:
            lines.extend("Unresolved: " + r["reason"] for r in unresolved)
        if not claims and not unresolved:
            lines.append("No supported answer is available from the supplied sources.")
        text = Template(config["template"]).substitute(body="\n\n".join(lines), title=data.get("title",config.get("title","Draft")))
        return ModuleResult({"text":text,"claim_ids":[c["id"] for c in claims],"citations":citations,"unresolved":unresolved,"synthetic":True})


class HumanReview(Module):
    def execute(self, context, inputs, config):
        return ModuleResult({"requested":True},"waiting_review")


def registry():
    r = Registry()
    for name, module in [("ingest",Ingestor()),("retrieve",Retriever()),("reason",ReasoningHead()),("syntax_check",SyntaxCheck()),("schema_check",SchemaCheck()),("content_check",ContentCheck()),("evidence_stage",EvidenceStage()),("evidence_finalize",EvidenceFinalize()),("render",Renderer()),("human_review",HumanReview())]:
        r.register(name,module)
    return r

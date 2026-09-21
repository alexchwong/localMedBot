"""Typed application-defined indexes over immutable corpus revisions."""
from __future__ import annotations
from datetime import date
import math,re
from .contracts import Fault, validate


def value_valid(value,spec):
    typ=spec["type"]
    valid={"string":lambda x:isinstance(x,str),"boolean":lambda x:isinstance(x,bool),"number":lambda x:isinstance(x,(int,float)) and not isinstance(x,bool),"date":lambda x:isinstance(x,str)}
    if typ not in valid or not valid[typ](value): raise Fault("index_type")
    if typ=="number" and not math.isfinite(value): raise Fault("index_type")
    if typ=="date":
        try: date.fromisoformat(value)
        except ValueError as exc: raise Fault("index_type") from exc
    if "values" in spec and value not in spec["values"]: raise Fault("index_vocabulary")

def indexes_valid(indexes,definitions):
    if not isinstance(indexes,dict) or set(indexes)-set(definitions): raise Fault("unknown_index")
    for key,spec in definitions.items():
        if key not in indexes:
            if spec.get("required"): raise Fault("missing_index",key)
            continue
        vals=indexes[key] if spec.get("many") else [indexes[key]]
        if spec.get("many") and not isinstance(vals,list): raise Fault("index_cardinality",key)
        for v in vals:value_valid(v,spec)

def prepare_sources(sources,profile,extract=None):
    if not isinstance(sources,list) or not sources or any(not isinstance(x,dict) for x in sources): raise Fault("empty_import")
    items=[]; source_ids=set(); item_ids=set()
    for source in sources:
        sid=source.get("id","")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}",sid) or sid in source_ids: raise Fault("source_id")
        source_ids.add(sid); kind=source.get("format"); content=source.get("content")
        if kind not in profile.get("formats",["json","text","markdown"]): raise Fault("source_format")
        if kind=="json":
            if not isinstance(content,dict) or not isinstance(content.get("items"),list): raise Fault("source_shape")
            chunks=[(f"/items/{i}",row) for i,row in enumerate(content["items"])]
        elif kind in {"text","markdown"} and isinstance(content,str):
            size=int(profile.get("chunk_chars",2000))
            if not 1<=size<=20000: raise Fault("chunk_size")
            chunks=[(f"chars:{o}:{min(o+size,len(content))}",content[o:o+size]) for o in range(0,len(content),size)] or [("chars:0:0","")]
        else: raise Fault("source_format")
        for i,(locator,chunk) in enumerate(chunks):
            if isinstance(chunk,dict): rows=[chunk]
            elif profile.get("mode")=="model":
                if extract is None: raise Fault("model_required")
                rows=extract(chunk)
            else: rows=[{"id":str(i),"text":chunk,"indexes":dict(profile.get("default_indexes",{}))}]
            for j,row in enumerate(rows):
                validate(row,profile["item_schema"]); indexes_valid(row["indexes"],profile["indexes"])
                eid=f"{sid}:{i}:{j}"
                if eid in item_ids: raise Fault("duplicate_item")
                item_ids.add(eid); items.append({**row,"id":eid,"source":sid,"locator":locator,"assertion_kind":"extracted_assertion" if profile.get("mode")=="model" and not isinstance(chunk,dict) else "excerpt"})
    return items

def source_passage(corpus,item):
    source=next(s for s in corpus["sources"] if s["id"]==item["source"]); loc=item["locator"]
    if loc.startswith("/items/"): value=source["content"]["items"][int(loc.split("/")[-1])]
    else:
        _,start,end=loc.split(":"); value=source["content"][int(start):int(end)]
    return {"source":source,"locator":loc,"passage":value,"corpus_revision":corpus["id"]}

def match_filter(item,filt,definitions):
    if not isinstance(filt,dict) or set(filt)-{"field","op","value"}: raise Fault("query_type")
    key,op,val=filt.get("field"),filt.get("op","eq"),filt.get("value")
    if key not in definitions: raise Fault("unknown_index",str(key))
    spec=definitions[key]
    if op not in {"eq","in","gte","lte"} or (op in {"gte","lte"} and spec["type"] not in {"number","date"}): raise Fault("unsupported_operator",op)
    if op=="in":
        if not isinstance(val,list): raise Fault("query_type")
        for v in val:value_valid(v,spec)
    else:value_valid(val,spec)
    if key not in item["indexes"]: return False
    values=item["indexes"][key] if spec.get("many") else [item["indexes"][key]]
    return any(v==val if op=="eq" else v in val if op=="in" else v>=val if op=="gte" else v<=val for v in values)

class Knowledge:
    def __init__(self,store,corpus_ids,overlay=None): self.store,self.ids,self.overlay=store,list(corpus_ids),overlay or {}
    def eligible(self,item):
        include,exclude=self.overlay.get("include"),self.overlay.get("exclude",[])
        return (include is None or item["id"] in include) and item["id"] not in exclude
    def read(self,cid,eid):
        if cid not in self.ids: raise Fault("scope_denied")
        corpus=self.store.corpus(cid)
        for item in corpus["items"]:
            if item["id"]==eid and self.eligible(item): return {**item,"revision":cid,"origin":source_passage(corpus,item)}
        raise Fault("evidence_not_found")
    def search(self,query="",filters=None,limit=12,mode="lexical"):
        if filters is not None and not isinstance(filters,list) or mode!="lexical" or not isinstance(query,str) or not isinstance(limit,int) or isinstance(limit,bool) or not 1<=limit<=50: raise Fault("query_type")
        terms=set(re.findall(r"\w+",query.casefold())); results=[]
        for cid in self.ids:
            corpus=self.store.corpus(cid)
            for filt in filters or []: match_filter({"indexes":{}},filt,corpus["profile"]["indexes"])
            for item in corpus["items"]:
                if not self.eligible(item) or not all(match_filter(item,f,corpus["profile"]["indexes"]) for f in filters or []): continue
                words=set(re.findall(r"\w+",item["text"].casefold())); score=len(terms&words)
                if terms and not score: continue
                results.append({**item,"revision":cid,"score":score})
        return sorted(results,key=lambda x:(-x["score"],x["revision"],x["id"]))[:limit]

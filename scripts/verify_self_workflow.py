#!/usr/bin/env python3
"""Orchestrate real frontier-session self handoffs with versioned synthetic inputs."""
from pathlib import Path
import argparse,json,sys
from localmedbot.service import Service
from localmedbot.contracts import Fault

ROOT=Path(__file__).resolve().parents[1]
LIVE=ROOT/'tests'/'fixtures'/'live'

def load(name): return json.loads((LIVE/name).read_text(encoding='utf-8'))
def write(path,value):
    if path: Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    else: print(json.dumps(value,ensure_ascii=False,indent=2))

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument('--data',default='state/self-acceptance'); p.add_argument('--runs-root',default='runs/self-acceptance'); p.add_argument('--workflow',choices=['clinical_letter','guideline_qa']); p.add_argument('--action',choices=['start','export','submit','status'],required=True); p.add_argument('--run-id'); p.add_argument('--response'); p.add_argument('--output'); a=p.parse_args(argv)
    s=None
    try:
        s=Service(ROOT/'applications',a.data,ROOT/'model_profiles',ROOT/'guideline_sets',ROOT/'tests/fixtures/steps',runs_root=a.runs_root)
        if a.action=='start':
            if not a.workflow: raise Fault('workflow_required')
            pid=f'{a.workflow}.self.default'
            if a.workflow=='clinical_letter':
                case=load('clinical_letter.synthetic-v1.json'); data=case['input']; sel=None
            else:
                suite=load('guideline_qa.synthetic-v1.json'); data={'question':suite['cases'][0]['question']}; sel=suite['guideline_selection']
            rid=s.start(a.workflow,pid,'free_text',data,guideline_selection=sel,developer=True); run=s.runner.advance(rid)
            result={'run_id':rid,'status':run['status'],'handoff':s.self_handoff(rid) if run['status']=='waiting_model' else None}
        elif a.action=='export':
            if not a.run_id: raise Fault('run_id_required')
            result=s.self_handoff(a.run_id)
        elif a.action=='submit':
            if not a.run_id or not a.response: raise Fault('response_required')
            env=json.loads(Path(a.response).read_text(encoding='utf-8')); run=s.self_submit(a.run_id,env); result={'run_id':a.run_id,'status':run['status'],'handoff':s.self_handoff(a.run_id) if run['status']=='waiting_model' else None}
        else:
            if not a.run_id: raise Fault('run_id_required')
            run=s.store.run(a.run_id); result={'run_id':a.run_id,'status':run['status'],'usage':run.get('usage'),'active':run.get('active'),'handoff':s.self_handoff(a.run_id) if run['status']=='waiting_model' else None}
        write(a.output,result); return 0
    except (Fault,OSError,ValueError,KeyError) as exc:
        print(json.dumps({'error':{'code':getattr(exc,'code','invalid_input'),'detail':getattr(exc,'detail','')}},ensure_ascii=False),file=sys.stderr); return 2
    finally:
        if s is not None: s.close()
if __name__=='__main__': raise SystemExit(main())

#!/usr/bin/env python3
"""Live OpenRouter/LM Studio acceptance using version-controlled synthetic cases."""
from __future__ import annotations
from pathlib import Path
import argparse,json,sys,time
from localmedbot.service import Service
from localmedbot.contracts import Fault

ROOT=Path(__file__).resolve().parents[1]
LIVE=ROOT/'tests'/'fixtures'/'live'

def load(name): return json.loads((LIVE/name).read_text(encoding='utf-8'))

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--provider',choices=['openrouter','lmstudio'],required=True)
    p.add_argument('--workflow',choices=['clinical_letter','guideline_qa','both'],default='both')
    p.add_argument('--profile'); p.add_argument('--model'); p.add_argument('--base-url')
    p.add_argument('--data',default='state/live-acceptance'); p.add_argument('--runs-root',default='runs/live-acceptance'); p.add_argument('--report',required=True)
    a=p.parse_args(argv)
    report={'schema_version':1,'provider':a.provider,'requested_workflow':a.workflow,'started':time.time(),'cases':[],'status':'fail'}
    service=None
    try:
        s=service=Service(ROOT/'applications',a.data,ROOT/'model_profiles',ROOT/'guideline_sets',ROOT/'tests/fixtures/steps',runs_root=a.runs_root)
        workflows=['clinical_letter','guideline_qa'] if a.workflow=='both' else [a.workflow]
        for workflow in workflows:
            pid=a.profile if a.profile and a.workflow!='both' else f'{workflow}.{a.provider}.default'
            overlay={}
            if a.model: overlay['model']=a.model
            if a.base_url: overlay['base_url']=a.base_url
            if overlay: s.configure_profile(pid,overlay)
            resolved=s.profiles.resolve(pid,{},workflow_id=workflow,runnable=True)
            if a.provider=='openrouter' and not s.profiles.credential(resolved):
                raise Fault('credential_missing','Set OPENROUTER_API_KEY or configure a process-memory credential.')
            probe=s.verify_provider(pid,workflow)
            report['cases'].append({'case':f'{workflow}.provider_probe','status':'pass' if probe['success'] else 'fail','returned_models':[x.get('returned_model') for x in probe['results']], 'errors':[x.get('error') for x in probe['results'] if x.get('error')]})
            if not probe['success']:
                first_error=next((x.get('error') for x in probe['results'] if x.get('error')), 'provider_probe_failed')
                raise Fault(first_error)
            if workflow=='clinical_letter':
                case=load('clinical_letter.synthetic-v1.json')
                rid=s.start(workflow,pid,'free_text',case['input']); run=s.runner.advance(rid)
                out=s.store.artifact(rid,'output')['payload'] if 'output' in run['active'] else {}
                ok=run['status']==case['rubric']['expected_terminal_state']
                report['cases'].append({'case':case['id'],'fixture_version':case['version'],'status':'pass' if ok else 'fail','run_status':run['status'],'claim_count':len(out.get('claim_ids',[])),'check_nodes':{k:run['nodes'].get(k) for k in ('facts_check','draft_check')}})
                if not ok: raise Fault('live_case_failed','clinical_letter')
                fixture=s.capture_fixture(rid,'draft',run['attempts']['draft']); fixture.update(id='live.letter.draft',version=1,data_suitability='synthetic',provenance={'source_run_id':None,'source_node_attempt':None})
                s.save_scratch_fixture(fixture)
                srid=s.run_step(workflow,'draft',fixture,pid,developer=True); step=s.runner.advance(srid)
                report['cases'].append({'case':'clinical_letter.draft.isolated','status':'pass' if step['status']=='completed' else 'fail','run_status':step['status']})
                if step['status']!='completed': raise Fault('live_step_failed','clinical_letter.draft')
            else:
                suite=load('guideline_qa.synthetic-v1.json')
                for case in suite['cases']:
                    rid=s.start(workflow,pid,'free_text',{'question':case['question']},guideline_selection=suite['guideline_selection']); run=s.runner.advance(rid)
                    out=s.store.artifact(rid,'output')['payload'] if 'output' in run['active'] else {}
                    ok=run['status']=='waiting_review' and out.get('evidence_outcome')==case['expected_evidence_outcome']
                    report['cases'].append({'case':case['id'],'fixture_version':suite['version'],'status':'pass' if ok else 'fail','run_status':run['status'],'evidence_outcome':out.get('evidence_outcome'),'conflict_count':len(out.get('conflict_ids',[]))})
                    if not ok: raise Fault('live_case_failed',case['id'])
                    if case['id']=='supported':
                        fixture=s.capture_fixture(rid,'reason',run['attempts']['reason']); fixture.update(id='live.guideline.reason',version=1,data_suitability='synthetic',provenance={'source_run_id':None,'source_node_attempt':None})
                        s.save_scratch_fixture(fixture)
                        srid=s.run_step(workflow,'reason',fixture,pid,developer=True); step=s.runner.advance(srid)
                        report['cases'].append({'case':'guideline_qa.reason.isolated','status':'pass' if step['status']=='completed' else 'fail','run_status':step['status']})
                        if step['status']!='completed': raise Fault('live_step_failed','guideline_qa.reason')
        report['status']='pass'; report['finished']=time.time(); Path(a.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8'); return 0
    except (Fault,OSError,ValueError,KeyError) as exc:
        report['status']='blocked' if getattr(exc,'code','') in {'credential_missing','endpoint_unreachable','provider_timeout'} else 'fail'
        report['error']={'code':getattr(exc,'code','invalid_input'),'detail':getattr(exc,'detail','')}; report['finished']=time.time()
        Path(a.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps(report['error']),file=sys.stderr); return 2
    finally:
        if service is not None: service.close()
if __name__=='__main__': raise SystemExit(main())

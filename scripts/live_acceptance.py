"""Execute real endpoints, retaining auditable runs; human review is still required."""
import argparse
import json
from pathlib import Path
import yaml
from localmedbot.service import Service


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--local-config',required=True)
    p.add_argument('--hosted-config',required=True)
    p.add_argument('--data',default='.localmedbot/live-acceptance')
    p.add_argument('--apps',default='applications')
    a=p.parse_args()
    results=[]
    for category,path,application in [('local',a.local_config,'clinical_letter'),('hosted',a.hosted_config,'guideline_qa')]:
        config=yaml.safe_load(Path(path).read_text())
        if config.get('provider')!='http':
            raise SystemExit('Live acceptance requires HTTP configurations, not recordings.')
        service=Service(a.apps,a.data,config)
        fixture=service.example(application,'standard')
        rid=service.start(application,fixture['input'])
        run=service.runner.advance(rid)
        results.append({'endpoint_category':category,'application':application,'run_id':rid,'status':run['status'],'model':config['model'],'usage':run['usage'],'human_review':'required','error':run.get('error')})
    print(json.dumps(results,indent=2))
    return 0 if all(r['status']=='waiting_review' for r in results) else 1

if __name__=='__main__':raise SystemExit(main())

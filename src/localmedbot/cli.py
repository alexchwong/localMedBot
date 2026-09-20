import argparse
import json
import sys
from pathlib import Path
import yaml
from .contracts import Fault
from .providers import make_provider
from .service import Service


def main(argv=None):
    parser=argparse.ArgumentParser(prog="localmedbot")
    parser.add_argument("--apps",default="applications")
    parser.add_argument("--data",default=".localmedbot")
    parser.add_argument("--config",help="YAML model configuration; credentials are environment-only")
    sub=parser.add_subparsers(dest="command",required=True)
    sub.add_parser("apps")
    sub.add_parser("check")
    p=sub.add_parser("ingest");p.add_argument("application");p.add_argument("--sources")
    p=sub.add_parser("run");p.add_argument("application");p.add_argument("--example",default="standard");p.add_argument("--input")
    for cmd in ["status","resume","cancel"]:
        p=sub.add_parser(cmd);p.add_argument("run_id")
    p=sub.add_parser("review");p.add_argument("run_id");p.add_argument("--revision",type=int,required=True);p.add_argument("--actor",required=True);p.add_argument("--decision",choices=["approve","reject","revise"],required=True);p.add_argument("--comments",default="")
    p=sub.add_parser("serve");p.add_argument("--port",type=int,default=8765)
    args=parser.parse_args(argv)
    try:
        config=yaml.safe_load(Path(args.config).read_text()) if args.config else None
        service=Service(args.apps,args.data,config)
        if args.command=="apps": result=service.applications()
        elif args.command=="check":
            for app in service.applications():service.load(app["id"])
            make_provider(service.model)
            result={"status":"valid","applications":len(service.applications()),"provider":service.model["provider"]}
        elif args.command=="ingest":
            sources=json.loads(Path(args.sources).read_text()) if args.sources else None
            result={"corpus_id":service.ingest(args.application,sources)}
        elif args.command=="run":
            input_value=json.loads(Path(args.input).read_text()) if args.input else None
            rid=service.start(args.application,input_value,args.example)
            result=service.runner.advance(rid)
        elif args.command=="status":result=service.inspect(args.run_id)
        elif args.command=="resume":result=service.runner.advance(args.run_id)
        elif args.command=="cancel":service.runner.cancel(args.run_id);result={"status":"cancelled"}
        elif args.command=="review":result=service.runner.review(args.run_id,args.revision,args.actor,args.decision,args.comments)
        else:
            from .web import create_app
            create_app(service).run(host="127.0.0.1",port=args.port,debug=False,threaded=True)
            return 0
        print(json.dumps(result,indent=2,ensure_ascii=False))
        return 1 if isinstance(result,dict) and result.get("status") in ["failed","blocked"] else 0
    except (Fault,OSError,ValueError,KeyError) as exc:
        print(json.dumps({"error":{"code":getattr(exc,"code","invalid_input"),"detail":getattr(exc,"detail","")}}),file=sys.stderr)
        return 1

if __name__=="__main__":
    raise SystemExit(main())

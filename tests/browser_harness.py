"""Real-browser harness that bridges browser fetches into Flask without TCP.

Some verification sandboxes apply Chromium policy that blocks loopback navigation.
This harness still executes the shipped HTML/CSS/JavaScript in Chromium and sends
its fetch requests through Flask's real test client, preserving the browser/API
contract while avoiding the environment's network policy.
"""
from __future__ import annotations
from pathlib import Path


def mount_browser_ui(page, app, repo_root: Path):
    client=app.test_client()

    def request(payload):
        path=str(payload.get("path") or "/")
        method=str(payload.get("method") or "GET").upper()
        headers={str(k):str(v) for k,v in (payload.get("headers") or {}).items()}
        body=payload.get("body")
        response=client.open(path,method=method,headers=headers,data=body)
        return {"status":response.status_code,"body":response.get_data(as_text=True)}

    page.expose_function("__localMedBotFetch",request)
    response=client.get("/")
    if response.status_code != 200:
        raise RuntimeError(f"UI bootstrap failed with HTTP {response.status_code}")
    html=response.get_data(as_text=True)
    css=(repo_root/"src/localmedbot/static/app.css").read_text(encoding="utf-8")
    javascript=(repo_root/"src/localmedbot/static/app.js").read_text(encoding="utf-8")
    bridge="""
<script>
if(!crypto.randomUUID){crypto.randomUUID=()=>{const b=new Uint8Array(16);crypto.getRandomValues(b);b[6]=(b[6]&15)|64;b[8]=(b[8]&63)|128;const h=[...b].map(x=>x.toString(16).padStart(2,'0')).join('');return `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`;};}
window.fetch=async function(path,options){
  options=options||{};
  const reply=await window.__localMedBotFetch({
    path:String(path), method:options.method||'GET', headers:options.headers||{}, body:options.body??null
  });
  return {
    ok:reply.status>=200&&reply.status<300,
    status:reply.status,
    json:async()=>JSON.parse(reply.body||'{}'),
    text:async()=>reply.body||''
  };
};
</script>
"""
    html=html.replace('<link rel="stylesheet" href="/static/app.css">',f"<style>{css}</style>")
    html=html.replace('<script src="/static/app.js" defer></script>',"")
    html=html.replace("</body>",bridge+"<script>"+javascript+"</script></body>")
    page.set_content(html,wait_until="load")
    page.wait_for_selector("#workflow")
    page.wait_for_function("()=>document.querySelector('#workflow').options.length>0",timeout=10000)
    return client

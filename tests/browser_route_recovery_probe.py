"""Minimal local probe for lost-response routing; no Orbis or financial services."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from threading import Thread
from pathlib import Path
import argparse,json
from playwright.sync_api import sync_playwright
from tests.browser_network import route_once


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=('retained','original'),default='retained')
    parser.add_argument('--output',type=Path,required=True)
    options=parser.parse_args();counts={'post':0,'get':0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path.startswith('/ping'):counts['get']+=1
            body=b'<!doctype html><title>Network probe</title>' if self.path=='/' else b'{"ok":true}'
            self.send_response(200);self.send_header('Content-Type','text/html' if self.path=='/' else 'application/json')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length',0)));counts['post']+=1;body=b'{"accepted":true}'
            self.send_response(202);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    result={'mode':options.mode,'iterations':60,'completed':0,'counts':counts,'passed':False}
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            try:
                page=browser.new_page();page.goto('http://127.0.0.1:'+str(server.server_port)+'/')
                for i in range(result['iterations']):
                    def lose(route):
                        response=route.fetch();assert response.status==202;route.abort('failed')
                    pattern='**/start?i='+str(i)
                    if options.mode=='original':page.route(pattern,lose,times=1)
                    else:route_once(page,pattern,lose)
                    page.evaluate("""i=>{window.done=null;fetch('/start?i='+i,{method:'POST',body:'{}'}).catch(()=>fetch('/ping?i='+i)).then(()=>{window.done=true;});}""",i)
                    page.wait_for_function('window.done===true',timeout=2500)
                    result['completed']+=1
                assert counts=={'post':60,'get':60};result['passed']=True
            finally:browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(3)
        options.output.write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)


if __name__=='__main__':main()

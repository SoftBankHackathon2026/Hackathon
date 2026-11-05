import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = os.environ.get('VERSION', 'v1')
RELEASE = os.environ.get('LAUNCHPAD_RELEASE_ID', 'unassigned')

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        healthy = VERSION != 'broken'
        status = 503 if self.path == '/healthz' and not healthy else 200
        data = json.dumps({'service':'Oneport Fargate Demo','version':VERSION,'release':RELEASE,'healthy':healthy}).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('X-Launchpad-Release', RELEASE)
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)

ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()

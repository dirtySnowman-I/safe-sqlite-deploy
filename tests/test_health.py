import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import unittest
from safe_sqlite_deploy.health import wait_health
from safe_sqlite_deploy.util import DeployError

class HealthTests(unittest.TestCase):
    def test_healthy_old_revision_or_wrong_instance_is_not_success(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(200);self.end_headers()
                self.wfile.write(json.dumps({'status':'ok','database':'ok','revision':'old','instance':'old-instance'}).encode())
        server=HTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        url='http://127.0.0.1:'+str(server.server_port)+'/health'
        try:
            with self.assertRaises(DeployError):wait_health(url,'new','new-instance',0.15)
            with self.assertRaises(DeployError):wait_health(url,'old','new-instance',0.15)
            self.assertEqual(wait_health(url,'old','old-instance',0.2)['status'],'ok')
        finally:server.shutdown();server.server_close();worker.join()

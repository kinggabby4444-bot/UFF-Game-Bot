import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


port = int(os.environ.get("PORT", 10000))

server = HTTPServer(("0.0.0.0", port), HealthHandler)

print(f"Health server running on port {port}")

server.serve_forever()

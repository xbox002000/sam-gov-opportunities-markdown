"""Tiny local mock of SAM.gov Opportunities API v2 for local_test.sh / E2E."""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(__file__)
FX = lambda n: json.load(open(os.path.join(HERE, "fixtures", n)))  # noqa: E731


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter
        pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        key = (q.get("api_key") or [""])[0]
        if "noticedesc" in u.path:
            if not key:
                return self._json(404, FX("error_404_nokey.json"))
            return self._json(200, FX("noticedesc.json"))
        # search
        if not key:
            return self._json(404, FX("error_404_nokey.json"))
        if key == "BAD":
            return self._json(404, FX("error_404_badkey.json"))
        return self._json(200, FX("search_ok.json"))

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    port = int(os.environ.get("PORT", "8765"))
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    print(f"mock SAM on http://127.0.0.1:{port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()

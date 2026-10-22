import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .pipeline import answer, extract
from .store import Store


STATIC = Path(__file__).parent / "static"


def make_handler(database):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, body):
            payload = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            params = parse_qs(parsed.query)
            if path in ("/", "/index.html", "/style.css", "/app.js"):
                file = STATIC / ("index.html" if path == "/" else path.lstrip("/"))
                payload = file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}[file.suffix] + "; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            try:
                store = Store(database)
                try:
                    if path == "/api/stats":
                        self.respond(200, store.stats())
                    elif path == "/api/resolutions":
                        self.respond(200, store.pending())
                    elif path == "/api/graph":
                        self.respond(200, store.graph(params.get("q", [""])[0]))
                    elif path == "/api/documents":
                        self.respond(200, store.list_documents(params.get("q", [""])[0],
                                                                params.get("limit", [25])[0],
                                                                params.get("offset", [0])[0]))
                    elif path.startswith("/api/documents/"):
                        document = store.document(path.removeprefix("/api/documents/"))
                        self.respond(200, document) if document else self.respond(404, {"error": "Document not found"})
                    else:
                        self.respond(404, {"error": "Not found"})
                finally:
                    store.close()
            except (ValueError, TypeError) as exc:
                self.respond(400, {"error": str(exc)})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 2_000_000:
                    raise ValueError("Request too large")
                data = json.loads(self.rfile.read(length))
                store = Store(database)
                try:
                    path = urlparse(self.path).path
                    if path == "/api/ingest":
                        facts = data.get("facts")
                        if facts is None:
                            facts = extract(data["text"])
                        self.respond(200, store.ingest(data["title"], data["text"], facts))
                    elif path == "/api/ask":
                        self.respond(200, answer(store, data["question"], data.get("mode", "auto"),
                                                 int(data.get("max_hops", 3))))
                    elif path == "/api/resolve":
                        store.resolve(data["id"], data["action"])
                        self.respond(200, {"ok": True})
                    else:
                        self.respond(404, {"error": "Not found"})
                finally:
                    store.close()
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                self.respond(400, {"error": str(exc)})
            except Exception as exc:
                self.respond(500, {"error": str(exc)})
    return Handler


def serve(database, host="127.0.0.1", port=8000):
    server = ThreadingHTTPServer((host, port), make_handler(database))
    print(f"GraphRAG Lite: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

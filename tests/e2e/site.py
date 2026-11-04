"""Synthetic search app with a toggleable 'stale results' bug (engine examples/search_bug.html)."""

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html><meta charset="utf-8"><title>검색</title>
<input id="query"><button id="search">Search</button>
<div id="result">empty</div><div id="ready">ready</div>
<script>
document.querySelector('#search').onclick = () => {
  const q = document.querySelector('#query').value;
  if (q === 'cat') document.querySelector('#result').textContent = 'cat';
  FIX
  document.querySelector('#ready').textContent = 'done';
};
</script>"""


@contextmanager
def search_site():
    state = {"fixed": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            fix = "else document.querySelector('#result').textContent = 'empty';"
            body = PAGE.replace("FIX", fix if state["fixed"] else "").encode()
            status = 200 if self.path.startswith("/app/") else 404
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()

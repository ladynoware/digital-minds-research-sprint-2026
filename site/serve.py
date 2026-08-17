#!/usr/bin/env python3
"""Serve the site locally, without the browser caching your edits away.

`python -m http.server` sends no cache headers at all, so browsers apply their
own heuristic and quietly keep serving an old CSS or JS file — you edit, reload,
and see nothing change. That is a miserable way to art-direct anything, so this
adds `Cache-Control: no-store` and nothing else.

    python site/serve.py            # http://localhost:8765
    python site/serve.py 9000       # another port
"""

from __future__ import annotations

import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8765


class NoCacheHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # One quiet line per request; the default prefixes every line with the
        # client address, which is always ::1 here.
        #
        # Flushed explicitly, because a piped stderr is block-buffered and the
        # log would otherwise appear late or not at all. Guarded, because if
        # whatever is reading that pipe goes away the write raises — and losing
        # a log line is never a reason for the server to fall over mid-request.
        try:
            sys.stderr.write(f"  {fmt % args}\n")
            sys.stderr.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass


def main(argv: list[str]) -> int:
    port = int(argv[1]) if len(argv) > 1 else DEFAULT_PORT
    handler = partial(NoCacheHandler, directory=str(SITE_DIR))
    # Threading, not plain HTTPServer: browsers open several parallel
    # connections and hold them open with keep-alive, and a single-threaded
    # server serves one at a time — so one idle connection wedges the whole
    # site and every later request hangs. That is not a hypothetical; it
    # happened.
    server = ThreadingHTTPServer(("localhost", port), handler)
    server.daemon_threads = True
    print(f"Serving {SITE_DIR.name}/ at http://localhost:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

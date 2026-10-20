#!/usr/bin/env python3
# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

"""Temporary loopback bridge for Google consent on a private MailIntel host.

Run on the computer where your browser runs, with Python 3.10 or newer:
  python3 google_oauth_localhost.py --upstream https://mail.example.com
Close with Ctrl+C after Google connects. Background sync needs no bridge.
"""
import argparse
import ssl
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True, help="MailIntel HTTPS origin, reachable from this computer")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ca-file", help="Optional PEM CA certificate if your private CA is not in Python's trust store")
    args = parser.parse_args()
    upstream = args.upstream.rstrip("/")
    uri = urlsplit(upstream)
    if uri.scheme != "https" or not uri.hostname or uri.username or uri.password or uri.path or uri.query or uri.fragment:
        parser.error("--upstream must be an HTTPS origin with no path or credentials")
    context = ssl.create_default_context(cafile=args.ca_file)
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
    local_origin = f"http://localhost:{args.port}"
    excluded = {"host", "connection", "transfer-encoding", "content-length", "accept-encoding", "upgrade", "proxy-connection"}

    class Bridge(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Authorization codes and tokens must not enter console logs.

        def proxy(self):
            if self.headers.get("Host") != f"localhost:{args.port}" or self.headers.get("Origin", local_origin) != local_origin:
                self.send_error(403, "Open the bridge through localhost")
                return
            if not self.path.startswith("/") or self.path.startswith("//"):
                self.send_error(400)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > 2 * 1024 * 1024:
                    self.send_error(413)
                    return
                headers = {k: v for k, v in self.headers.items() if k.lower() not in excluded}
                body = self.rfile.read(size) if size else None
                request = urllib.request.Request(upstream + self.path, data=body, headers=headers, method=self.command)
                try:
                    response = opener.open(request, timeout=60)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    content = response.read()
                    self.send_response(response.code)
                    for key, value in response.headers.items():
                        if key.lower() not in excluded:
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
            except (OSError, ValueError):
                self.send_error(502, "Cannot reach MailIntel. Check connectivity and CA trust; use --ca-file if needed.")

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = proxy

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Bridge)
    print(f"Open {local_origin}/#/mail-settings and sign in to MailIntel.")
    print(f"Google callback: {local_origin}/oauth/google/callback")
    print("After Google connects, press Ctrl+C. Background sync runs without this bridge.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

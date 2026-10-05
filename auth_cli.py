#!/usr/bin/env python
"""
One-time Kroger OAuth. Opens a browser, catches the callback on localhost:8734,
and stores the refresh token. Run once; the refresh token carries forward.

    python auth_cli.py

Requires http://localhost:8734/callback registered as a redirect URI in the
Kroger developer portal (already registered for these credentials).
"""

import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import kroger
from db import init_db

_result: dict = {}


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        _result["code"] = params.get("code", [None])[0]
        _result["error"] = params.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        message = "Kroger login failed — check the terminal." if _result["error"] else "Kroger connected. You can close this tab."
        self.wfile.write(f"<h2>{message}</h2>".encode())

    def log_message(self, *args):
        pass


def main() -> None:
    init_db()

    if not kroger.credentials_configured():
        raise SystemExit("Missing KROGER_CLIENT_ID / KROGER_CLIENT_SECRET in .env")

    url = kroger.authorize_url()
    print(f"Opening browser for Kroger login.\nIf it doesn't open, visit:\n{url}\n")
    webbrowser.open(url)

    server = HTTPServer(("localhost", kroger.REDIRECT_PORT), CallbackHandler)
    server.handle_request()
    server.server_close()

    if _result.get("error") or not _result.get("code"):
        raise SystemExit(f"Auth failed: {_result.get('error') or 'no code returned'}")

    kroger.exchange_code_for_token(_result["code"])
    print("Connected. Refresh token stored in shopper.db.")

    location_id = kroger.get_location_id()
    print(f"Store location: {location_id or 'could not resolve — check SHOPPER_ZIP'}")


if __name__ == "__main__":
    main()

import json
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from media_agent.agent import ask_agent, build_agent
from media_agent.progress import progress_after, reset_progress


HOST = "127.0.0.1"
PORT = 8765
ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "web_chat.html"
TOKEN = secrets.token_urlsafe(24)
AGENT_LOCK = threading.Lock()


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "MediaAgent/1.0"

    def log_message(self, format, *args):
        return

    def _headers(self, status=200, content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        self.end_headers()

    def _json(self, payload, status=200):
        self._headers(status)
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _authorized(self):
        return secrets.compare_digest(self.headers.get("X-Chat-Token", ""), TOKEN)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/progress":
            if not self._authorized():
                self._json({"error": "Invalid local chat token"}, 401)
                return
            try:
                after = int(self.headers.get("X-Progress-After", "0"))
            except ValueError:
                after = 0
            self._json(progress_after(max(0, after)))
            return
        if path != "/":
            self._json({"error": "Not found"}, 404)
            return
        page = PAGE.read_text(encoding="utf-8").replace("__CHAT_TOKEN__", TOKEN)
        self._headers(200, "text/html; charset=utf-8")
        self.wfile.write(page.encode("utf-8"))

    def do_POST(self):
        if not self._authorized():
            self._json({"error": "Invalid local chat token"}, 401)
            return
        if self.path == "/api/shutdown":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path != "/api/chat":
            self._json({"error": "Not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 50_000:
                raise ValueError("Message is empty or too large")
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            prompt = str(request.get("message", "")).strip()
            if not prompt:
                raise ValueError("Enter a message")
            with AGENT_LOCK:
                reset_progress(prompt)
                answer = ask_agent(self.server.agent, prompt)
            self._json({"answer": answer})
        except (ValueError, json.JSONDecodeError) as error:
            self._json({"error": str(error)}, 400)
        except Exception as error:
            self._json({"error": f"Agent error: {error}"}, 500)


def main():
    if "--check" in sys.argv:
        if not PAGE.exists() or "__CHAT_TOKEN__" not in PAGE.read_text(encoding="utf-8"):
            raise SystemExit("Web chat files are incomplete.")
        print("Web chat files are valid.")
        return

    print("Starting Media Agent browser chat...")
    print("The local page is available only on this computer.")
    server = ThreadingHTTPServer((HOST, PORT), ChatHandler)
    server.agent = build_agent()
    address = f"http://{HOST}:{PORT}/"
    threading.Timer(0.4, lambda: webbrowser.open(address)).start()
    print(f"Opened {address}")
    print("Keep this window open. Use the Exit button in the browser to close.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Media Agent closed.")


if __name__ == "__main__":
    main()

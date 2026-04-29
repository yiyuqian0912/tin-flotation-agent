#!/usr/bin/env python3
"""
server.py — HTTP server with SSE streaming progress.
Serves index.html + provides REST/SSE API wrapping agent.py.

Endpoints:
    GET  /                → index.html
    POST /api/chat        → SSE stream: progress events + final reply
    POST /api/reset       → reset session
    POST /api/reload      → clear skill cache
    GET  /api/status      → database status
"""

import argparse
import http.server
import json
import os
import queue
import sys
import threading
import traceback
import webbrowser

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# Load .env file if exists
_env_path = os.path.join(SCRIPT_DIR, ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from agent import agent_chat, execute_tool, SYSTEM_PROMPT, clear_skill_cache, DEEPSEEK_API_KEY
import agent as agent_module

sessions = {}
session_skills = {}


class Handler(http.server.SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SCRIPT_DIR, **kwargs)

    def do_POST(self):
        if self.path == "/api/chat":
            self._handle_chat_sse()
        elif self.path == "/api/reset":
            self._handle_reset()
        elif self.path == "/api/reload":
            self._handle_reload()
        else:
            self.send_error(404)

    def do_GET(self):
        if self.path == "/api/status":
            self._handle_status()
        else:
            super().do_GET()

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length > 0 else {}

    def _handle_chat_sse(self):
        """Stream progress events via SSE, then final reply."""
        try:
            body = self._read_body()
            message = body.get("message", "").strip()
            session_id = body.get("session_id", "default")

            if not message:
                self._json_response({"error": "Empty message"}, 400)
                return

            # SSE headers
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self._cors()
            self.end_headers()

            # Progress queue
            progress_q = queue.Queue()

            def on_progress(event_type, data):
                progress_q.put({"type": event_type, "data": data})

            def send_sse(event, data):
                try:
                    line = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    pass

            # Get or create session
            if session_id not in sessions:
                sessions[session_id] = [{"role": "system", "content": agent_module.SYSTEM_PROMPT}]
                session_skills[session_id] = set()

            history = sessions[session_id]
            loaded = session_skills[session_id]

            # Run agent in thread, collect progress
            result_holder = [None, None]  # [reply, error]

            def run_agent():
                try:
                    reply, hist = agent_chat(
                        message, history,
                        _loaded_skills=loaded,
                        on_progress=on_progress,
                    )
                    result_holder[0] = reply
                    sessions[session_id] = hist
                    session_skills[session_id] = loaded
                except Exception as e:
                    result_holder[1] = str(e)
                    traceback.print_exc()
                finally:
                    progress_q.put(None)  # sentinel

            t = threading.Thread(target=run_agent, daemon=True)
            t.start()

            # Stream progress events to client
            while True:
                item = progress_q.get()
                if item is None:
                    break
                send_sse("progress", item)

            # Send final result and close
            if result_holder[1]:
                send_sse("error", {"message": result_holder[1]})
            else:
                send_sse("done", {"reply": result_holder[0] or ""})

            # Signal end of stream
            try:
                self.wfile.write(b"event: close\ndata: {}\n\n")
                self.wfile.flush()
            except Exception:
                pass

        except Exception as e:
            traceback.print_exc()

    def _handle_reset(self):
        body = self._read_body()
        sid = body.get("session_id", "default")
        sessions.pop(sid, None)
        session_skills.pop(sid, None)
        self._json_response({"status": "reset"})

    def _handle_reload(self):
        clear_skill_cache()
        sessions.clear()
        session_skills.clear()
        self._json_response({"status": "reloaded"})

    def _handle_status(self):
        try:
            result = execute_tool("list_database_status", {})
            self._json_response(json.loads(result))
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def log_message(self, format, *args):
        msg = format % args
        if "/api/" in msg or "404" in msg or "500" in msg:
            sys.stderr.write(f"  {msg}\n")


def main():
    parser = argparse.ArgumentParser(description="Flotation UI Server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    # Check API key
    if not agent_module.DEEPSEEK_API_KEY:
        print("\n  ❌ DEEPSEEK_API_KEY 未设置。")
        print("  方式 1: 创建 .env 文件 (cp .env.example .env)")
        print("  方式 2: export DEEPSEEK_API_KEY='sk-your-key'\n")
        sys.exit(1)

    # Ensure data directories
    for d in [agent_module.RECIPES_DIR, agent_module.DATASETS_DIR, agent_module.FAISS_DIR]:
        os.makedirs(d, exist_ok=True)

    http.server.HTTPServer.allow_reuse_address = True
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)

    print(f"")
    print(f"  ⛏️  浮选助手")
    print(f"  ─────────────────────────")
    print(f"  界面:  http://localhost:{args.port}")
    print(f"  模型:  DeepSeek ({agent_module.MODEL})")
    print(f"  技能:  {SCRIPT_DIR}/skills/")
    print(f"  数据:  {SCRIPT_DIR}/database/")
    print(f"  ─────────────────────────")
    print(f"  Ctrl+C 停止\n")

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。")
        server.server_close()


if __name__ == "__main__":
    main()

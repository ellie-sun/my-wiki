#!/usr/bin/env python3
"""로컬 Claude Code 세션 대시보드.

~/.claude 아래의 세션 기록(projects/*/*.jsonl)과 실행 중인 세션 목록(sessions/*.json)을
읽어서 브라우저 대시보드로 보여준다. 표준 라이브러리만 쓰고, 127.0.0.1에만 열리며,
외부로 아무것도 보내지 않는다. 파일은 읽기만 한다.

    python3 claude_sessions.py                 # http://127.0.0.1:8765 에서 열기
    python3 claude_sessions.py --html out.html # 현재 상태를 HTML 파일 하나로 저장
    python3 claude_sessions.py --json          # 수집한 데이터를 JSON으로 출력
"""

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "dashboard.html"
TEXT_LIMIT = 280
STALE_DAYS = 14


def default_claude_dirs():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return [Path(env).expanduser()] if env else [Path.home() / ".claude"]


def clip(text, limit=TEXT_LIMIT):
    if not text:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def text_of(content):
    """메시지 content에서 사람이 읽는 텍스트만 꺼낸다."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def has_block(content, kind):
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == kind for b in content)


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def scan_transcript(path):
    """세션 기록 파일 하나를 요약한다. 대화가 하나도 없으면 None."""
    s = {
        "id": path.stem,
        "ai_title": None,
        "custom_title": None,
        "summary": None,
        "first_prompt": None,
        "last_prompt": None,
        "last_reply": None,
        "cwd": None,
        "branch": None,
        "entrypoint": None,
        "version": None,
        "model": None,
        "prompts": 0,
        "last_role": None,
        "pending_tool": False,
    }
    started = updated = None
    output_tokens = {}

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            t = o.get("type")
            if t == "ai-title":
                s["ai_title"] = o.get("aiTitle") or s["ai_title"]
                continue
            if t == "custom-title":
                s["custom_title"] = o.get("customTitle") or o.get("title") or s["custom_title"]
                continue
            if t == "summary":
                s["summary"] = o.get("summary") or s["summary"]
                continue
            if t == "last-prompt":
                s["last_prompt"] = o.get("lastPrompt") or s["last_prompt"]
                continue
            if o.get("isSidechain"):
                continue

            ts = parse_ts(o.get("timestamp"))
            if ts:
                started = ts if started is None or ts < started else started
                updated = ts if updated is None or ts > updated else updated
            # 세션 도중 cd로 옮겨 다녀도 프로젝트는 처음 시작한 폴더로 본다
            s["cwd"] = s["cwd"] or o.get("cwd")
            s["branch"] = o.get("gitBranch") or s["branch"]
            s["version"] = o.get("version") or s["version"]
            if not s["entrypoint"] and o.get("entrypoint"):
                s["entrypoint"] = o["entrypoint"]

            msg = o.get("message") if isinstance(o.get("message"), dict) else {}
            content = msg.get("content")
            if t == "user":
                if has_block(content, "tool_result") or "toolUseResult" in o:
                    s["pending_tool"] = False
                    s["last_role"] = "tool"
                    continue
                if o.get("isMeta"):
                    continue
                text = text_of(content).strip()
                # <command-name>, <local-command-stdout> 같은 내부 메시지는 사람이 쓴 요청이 아니다
                if not text or text.startswith("<"):
                    continue
                s["prompts"] += 1
                s["first_prompt"] = s["first_prompt"] or text
                s["last_user_text"] = text
                s["last_role"] = "user"
            elif t == "assistant":
                s["model"] = msg.get("model") or s["model"]
                usage = msg.get("usage") or {}
                if msg.get("id") and isinstance(usage.get("output_tokens"), int):
                    output_tokens[msg["id"]] = usage["output_tokens"]
                text = text_of(content).strip()
                if text:
                    s["last_reply"] = text
                if has_block(content, "tool_use"):
                    s["pending_tool"] = True
                elif text:
                    s["pending_tool"] = False
                s["last_role"] = "assistant"

    if s["prompts"] == 0:
        return None
    if updated is None:
        updated = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return {
        "id": s["id"],
        "title": clip(s["custom_title"] or s["ai_title"] or s["summary"] or s["first_prompt"], 90),
        "last_prompt": clip(s["last_prompt"] or s.get("last_user_text")),
        "last_reply": clip(s["last_reply"]),
        "cwd": s["cwd"],
        "project": Path(s["cwd"]).name if s["cwd"] else path.parent.name,
        "branch": s["branch"],
        "entrypoint": s["entrypoint"],
        "version": s["version"],
        "model": s["model"],
        "prompts": s["prompts"],
        "output_tokens": sum(output_tokens.values()),
        "started_at": iso(started),
        "updated_at": iso(updated),
        "last_role": s["last_role"],
        "pending_tool": s["pending_tool"],
        "file": str(path),
    }


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # Windows의 os.kill은 프로세스를 끝내 버리므로 쓰지 않는다
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_live(claude_dir):
    """sessions/*.json 중 프로세스가 아직 살아 있는 것만 sessionId별로 돌려준다."""
    live = {}
    folder = claude_dir / "sessions"
    if not folder.is_dir():
        return live
    for path in folder.glob("*.json"):
        try:
            o = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sid = o.get("sessionId")
        if not sid or not pid_alive(o.get("pid")):
            continue
        prev = live.get(sid)
        if prev and (prev.get("updatedAt") or 0) >= (o.get("updatedAt") or 0):
            continue
        live[sid] = o
    return live


def bucket_of(row, live):
    """상태 칸: working(작업 중) / needs_you(내 차례) / open(열려 있음) / closed(닫힘)."""
    if not live:
        return "closed", None
    status = str(live.get("status") or "").lower()
    if status == "busy":
        return "working", None
    if any(w in status for w in ("wait", "permission", "approv", "input", "block")):
        return "needs_you", "승인이나 입력을 기다려요"
    if row and row.get("pending_tool"):
        return "needs_you", "도구 실행 승인을 기다리는 것 같아요"
    if row and row.get("last_role") == "assistant":
        return "needs_you", "답을 마치고 다음 요청을 기다려요"
    return "open", None


class Scanner:
    def __init__(self, claude_dirs):
        self.claude_dirs = claude_dirs
        self.cache = {}
        self.lock = threading.Lock()

    def collect(self):
        with self.lock:
            rows = {}
            live = {}
            seen = set()
            for d in self.claude_dirs:
                live.update(read_live(d))
                projects = d / "projects"
                if not projects.is_dir():
                    continue
                for path in projects.glob("*/*.jsonl"):
                    key = str(path)
                    seen.add(key)
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    sig = (st.st_mtime_ns, st.st_size)
                    hit = self.cache.get(key)
                    if not hit or hit[0] != sig:
                        try:
                            hit = (sig, scan_transcript(path))
                        except OSError:
                            continue
                        self.cache[key] = hit
                    row = hit[1]
                    if row and (row["id"] not in rows or row["updated_at"] > rows[row["id"]]["updated_at"]):
                        rows[row["id"]] = row
            for key in list(self.cache):
                if key not in seen:
                    del self.cache[key]

            now = datetime.now(timezone.utc)
            out = []
            for sid in set(rows) | set(live):
                row = dict(rows.get(sid) or {
                    "id": sid, "title": None, "last_prompt": None, "last_reply": None,
                    "cwd": live[sid].get("cwd"), "project": Path(live[sid].get("cwd") or "?").name,
                    "branch": None, "entrypoint": None, "version": live[sid].get("version"), "model": None,
                    "prompts": 0, "output_tokens": 0, "started_at": None, "updated_at": None,
                    "last_role": None, "pending_tool": False, "file": None,
                })
                lv = live.get(sid)
                row["bucket"], row["reason"] = bucket_of(row, lv)
                row["live"] = bool(lv)
                if lv:
                    row["pid"] = lv.get("pid")
                    row["live_status"] = lv.get("status")
                    row["name"] = lv.get("name")
                    row["entrypoint"] = row["entrypoint"] or lv.get("entrypoint")
                    row["title"] = row["title"] or lv.get("name")
                    if lv.get("statusUpdatedAt"):
                        t = datetime.fromtimestamp(lv["statusUpdatedAt"] / 1000, timezone.utc)
                        if not row["updated_at"] or iso(t) > row["updated_at"]:
                            row["updated_at"] = iso(t)
                upd = parse_ts(row["updated_at"])
                row["stale"] = bool(not lv and upd and (now - upd).days >= STALE_DAYS)
                out.append(row)
            out.sort(key=lambda r: r["updated_at"] or "", reverse=True)
            return {
                "generated_at": iso(now),
                "claude_dirs": [str(d) for d in self.claude_dirs],
                "stale_days": STALE_DAYS,
                "sessions": out,
            }


def render_page(data=None):
    html = PAGE.read_text(encoding="utf-8")
    if data is not None:
        blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        html = html.replace("<!--SNAPSHOT-->", "<script>window.SNAPSHOT = " + blob + ";</script>", 1)
    return html


def make_handler(scanner, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, ctype):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            # 다른 사이트가 DNS rebinding으로 로컬 데이터를 읽지 못하게 Host를 확인한다
            if self.headers.get("Host") not in allowed_hosts:
                return self.send(403, "forbidden", "text/plain; charset=utf-8")
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self.send(200, render_page(), "text/html; charset=utf-8")
            if path == "/api/sessions":
                return self.send(200, json.dumps(scanner.collect(), ensure_ascii=False), "application/json; charset=utf-8")
            return self.send(404, "not found", "text/plain; charset=utf-8")

    return Handler


def main():
    ap = argparse.ArgumentParser(description="로컬 Claude Code 세션 대시보드")
    ap.add_argument("--claude-dir", action="append", type=Path,
                    help="세션 기록이 있는 Claude 설정 폴더 (기본: $CLAUDE_CONFIG_DIR 또는 ~/.claude). 여러 번 줄 수 있어요.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="브라우저를 자동으로 열지 않기")
    ap.add_argument("--html", type=Path, help="서버 대신 현재 상태를 HTML 파일로 저장")
    ap.add_argument("--json", action="store_true", help="수집한 데이터를 JSON으로 출력")
    args = ap.parse_args()

    dirs = [d.expanduser() for d in (args.claude_dir or default_claude_dirs())]
    missing = [str(d) for d in dirs if not (d / "projects").is_dir()]
    if missing:
        print("세션 기록 폴더(projects)를 찾지 못했어요: " + ", ".join(missing), file=sys.stderr)
    scanner = Scanner(dirs)

    if args.json:
        json.dump(scanner.collect(), sys.stdout, ensure_ascii=False, indent=2)
        print()
        return
    if args.html:
        args.html.write_text(render_page(scanner.collect()), encoding="utf-8")
        print(f"저장했어요: {args.html}")
        return

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(scanner, args.port))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"세션 대시보드: {url}  (끄려면 Ctrl+C)")
    if not args.no_open:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

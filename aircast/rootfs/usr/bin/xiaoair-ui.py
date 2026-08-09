#!/usr/bin/env python3
"""XiaoAir ingress UI: player dropdown + start/stop sample playback."""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

# Keep UI status polling from flooding addon logs.
os.environ.setdefault("XIAOAIR_QUIET", "1")

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
HA_API_URL = "http://supervisor/core/api"
SUPERVISOR_URL = "http://supervisor"
OPTIONS_FILE = "/data/options.json"
UI_PORT = int(os.environ.get("XIAOAIR_UI_PORT", "8099"))
TEST_PORT = int(os.environ.get("XIAOAIR_TEST_PORT", "7099"))
STATE_FILE = "/tmp/xiaoair_test_state.json"
SAMPLE_PATH = "/tmp/xiaoair_test.mp3"

_lock = threading.Lock()
_ffmpeg: Optional[subprocess.Popen] = None
_httpd_thread: Optional[threading.Thread] = None
_file_server: Optional[ThreadingHTTPServer] = None


def _load_media_players():
    spec = importlib.util.spec_from_file_location(
        "media_players", "/usr/bin/media-players.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load media-players.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_local_ip() -> str:
    try:
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        local_ip = sock.getsockname()[0]
        sock.close()
        return local_ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


def ha_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }


def load_options() -> Dict[str, Any]:
    try:
        with open(OPTIONS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def write_options_file(options: Dict[str, Any]) -> None:
    with open(OPTIONS_FILE, "w", encoding="utf-8") as handle:
        json.dump(options, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def supervisor_set_options(options: Dict[str, Any]) -> None:
    """Persist options through Supervisor so the Configuration tab stays in sync."""
    response = requests.post(
        f"{SUPERVISOR_URL}/addons/self/options",
        headers=ha_headers(),
        json={"options": options},
        timeout=20,
    )
    if response.status_code >= 400:
        # Some builds wrap payload differently; keep local file write as source of truth.
        print(
            f"warning: supervisor options update HTTP {response.status_code}: "
            f"{response.text[:300]}",
            flush=True,
        )
        response.raise_for_status()


def configured_media_player_raw() -> str:
    return str(load_options().get("media_player") or "").strip()


def list_players() -> Dict[str, Any]:
    mp = _load_media_players()
    choices = mp.list_media_player_choices()
    configured = configured_media_player_raw()
    resolved = resolve_target()
    return {
        "ok": True,
        "configured": configured,
        "selected": configured
        if configured and configured.lower() not in mp.AUTO_SENTINELS
        else "",
        "resolved_entity_id": resolved.get("entity_id"),
        "players": choices,
    }


def save_media_player(entity_id: str) -> Dict[str, Any]:
    entity_id = (entity_id or "").strip()
    mp = _load_media_players()
    if entity_id and entity_id.lower() not in mp.AUTO_SENTINELS:
        if not entity_id.startswith("media_player."):
            return {"ok": False, "error": "entity must be media_player.* or empty/auto"}
    else:
        entity_id = ""

    options = load_options()
    options["media_player"] = entity_id
    write_options_file(options)
    try:
        supervisor_set_options(options)
    except Exception as err:  # noqa: BLE001
        print(f"warning: supervisor options sync failed: {err}", flush=True)

    resolved = resolve_target()
    return {
        "ok": True,
        "configured": entity_id,
        "message": "已保存（空=自动识别）"
        if not entity_id
        else f"已保存: {entity_id}",
        "resolved_entity_id": resolved.get("entity_id"),
        "friendly_name": resolved.get("friendly_name"),
        "error": None if resolved.get("ok") else resolved.get("error"),
    }


def resolve_target() -> Dict[str, Any]:
    mp = _load_media_players()
    options = mp.load_options()
    players = mp.resolve_players(options)
    if not players:
        return {"ok": False, "error": "No media_player configured or auto-detected"}
    player = players[0]
    return {
        "ok": True,
        "entity_id": player["entity_id"],
        "friendly_name": player.get("friendly_name") or player["entity_id"],
        "airplay_name": str(options.get("airplay_name") or ""),
        "stream_format": str(options.get("stream_format") or "mp3"),
        "media_content_type": str(options.get("media_content_type") or "music"),
        "configured": str(options.get("media_player") or "").strip(),
    }


def read_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def write_state(data: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


def clear_state() -> None:
    try:
        os.remove(STATE_FILE)
    except FileNotFoundError:
        pass


def ensure_sample_mp3() -> None:
    if os.path.exists(SAMPLE_PATH) and os.path.getsize(SAMPLE_PATH) > 1000:
        return
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:duration=12:sample_rate=44100",
        "-f",
        "mp3",
        SAMPLE_PATH,
    ]
    print(f"generating sample audio: {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True, timeout=60)


def stop_file_server() -> None:
    global _file_server, _httpd_thread
    if _file_server is not None:
        try:
            _file_server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            _file_server.server_close()
        except Exception:  # noqa: BLE001
            pass
    _file_server = None
    _httpd_thread = None


def start_file_server() -> None:
    global _file_server, _httpd_thread
    stop_file_server()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[test-http] {self.address_string()} {fmt % args}", flush=True)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path not in ("/test.mp3", "/xiaoair_test.mp3"):
                self.send_error(404)
                return
            try:
                with open(SAMPLE_PATH, "rb") as handle:
                    body = handle.read()
            except OSError:
                self.send_error(500, "sample missing")
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    _file_server = ThreadingHTTPServer(("0.0.0.0", TEST_PORT), Handler)
    _httpd_thread = threading.Thread(target=_file_server.serve_forever, daemon=True)
    _httpd_thread.start()


def stop_ffmpeg() -> None:
    global _ffmpeg
    if not _ffmpeg:
        return
    try:
        os.killpg(_ffmpeg.pid, signal.SIGTERM)
    except Exception:  # noqa: BLE001
        try:
            _ffmpeg.terminate()
        except Exception:  # noqa: BLE001
            pass
    try:
        _ffmpeg.wait(timeout=3)
    except Exception:  # noqa: BLE001
        try:
            os.killpg(_ffmpeg.pid, signal.SIGKILL)
        except Exception:  # noqa: BLE001
            pass
    _ffmpeg = None


def play_media(entity_id: str, url: str, content_type: str) -> None:
    response = requests.post(
        f"{HA_API_URL}/services/media_player/play_media",
        headers=ha_headers(),
        json={
            "entity_id": entity_id,
            "media_content_id": url,
            "media_content_type": content_type,
        },
        timeout=15,
    )
    response.raise_for_status()


def stop_media(entity_id: str) -> None:
    response = requests.post(
        f"{HA_API_URL}/services/media_player/media_stop",
        headers=ha_headers(),
        json={"entity_id": entity_id},
        timeout=15,
    )
    response.raise_for_status()


def start_test() -> Dict[str, Any]:
    with _lock:
        target = resolve_target()
        if not target.get("ok"):
            return target

        stop_ffmpeg()
        stop_file_server()

        ensure_sample_mp3()
        start_file_server()
        time.sleep(0.2)

        entity_id = str(target["entity_id"])
        content_type = str(target["media_content_type"])
        stream_url = f"http://{get_local_ip()}:{TEST_PORT}/test.mp3"
        play_media(entity_id, stream_url, content_type)
        write_state(
            {
                "testing": True,
                "entity_id": entity_id,
                "stream_url": stream_url,
                "started_at": time.time(),
            }
        )
        print(f"test started: {entity_id} <- {stream_url}", flush=True)
        return {
            "ok": True,
            "testing": True,
            "entity_id": entity_id,
            "friendly_name": target.get("friendly_name"),
            "configured": target.get("configured"),
            "stream_url": stream_url,
            "message": "Sample audio play_media sent",
        }


def stop_test() -> Dict[str, Any]:
    with _lock:
        state = read_state()
        entity_id = str(state.get("entity_id") or "")
        if not entity_id:
            target = resolve_target()
            entity_id = str(target.get("entity_id") or "")

        stop_ffmpeg()
        stop_file_server()

        if entity_id:
            try:
                stop_media(entity_id)
            except Exception as err:  # noqa: BLE001
                clear_state()
                return {
                    "ok": False,
                    "testing": False,
                    "entity_id": entity_id,
                    "error": f"media_stop failed: {err}",
                }

        clear_state()
        print(f"test stopped: {entity_id or '(none)'}", flush=True)
        return {
            "ok": True,
            "testing": False,
            "entity_id": entity_id or None,
            "message": "Stopped",
        }


def status() -> Dict[str, Any]:
    target = resolve_target()
    state = read_state()
    testing = bool(state.get("testing"))
    players = list_players()
    result: Dict[str, Any] = {
        "ok": True,
        "testing": testing,
        "stream_url": state.get("stream_url"),
        "ui_port": UI_PORT,
        "test_port": TEST_PORT,
        "configured": players.get("configured") or "",
        "selected": players.get("selected") or "",
        "players": players.get("players") or [],
    }
    if target.get("ok"):
        result.update(
            {
                "entity_id": target["entity_id"],
                "friendly_name": target.get("friendly_name"),
                "airplay_name": target.get("airplay_name"),
            }
        )
    else:
        result["ok"] = False
        result["error"] = target.get("error")
    if testing and state.get("entity_id"):
        result["entity_id"] = state.get("entity_id")
    return result


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>XiaoAir</title>
  <style>
    :root {
      --bg: #111318;
      --card: #1c1f26;
      --text: #e8eaed;
      --muted: #9aa0a6;
      --accent: #03a9f4;
      --danger: #ef5350;
      --ok: #66bb6a;
      --border: #2a2f3a;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      padding: 24px;
    }
    .card {
      max-width: 720px;
      margin: 0 auto;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px 22px;
    }
    h1 { font-size: 1.25rem; margin: 0 0 6px; }
    p { color: var(--muted); margin: 0 0 14px; line-height: 1.45; font-size: 0.95rem; }
    label { display: block; font-size: 0.9rem; margin-bottom: 6px; color: var(--muted); }
    select {
      width: 100%;
      background: #0d0f14;
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      font-size: 0.95rem;
    }
    .row { display: flex; gap: 10px; flex-wrap: wrap; margin: 14px 0; }
    button {
      border: 0;
      border-radius: 8px;
      padding: 10px 16px;
      font-size: 0.95rem;
      cursor: pointer;
      color: #fff;
    }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    .start { background: var(--accent); }
    .stop { background: var(--danger); }
    .save { background: #5c6bc0; }
    .meta {
      font-family: ui-monospace, Consolas, monospace;
      font-size: 0.82rem;
      background: #0d0f14;
      border-radius: 8px;
      padding: 12px;
      white-space: pre-wrap;
      word-break: break-all;
      border: 1px solid var(--border);
      min-height: 5.5em;
    }
    .ok { color: var(--ok); }
    .err { color: var(--danger); }
    .logbox {
      font-family: ui-monospace, Consolas, monospace;
      font-size: 0.78rem;
      background: #0d0f14;
      border-radius: 8px;
      padding: 12px;
      white-space: pre-wrap;
      word-break: break-all;
      border: 1px solid var(--border);
      min-height: 12em;
      max-height: 22em;
      overflow: auto;
      color: #cfd3da;
      margin-top: 8px;
    }
    h2 { font-size: 1rem; margin: 18px 0 4px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>XiaoAir</h1>
    <p>选择目标音箱，并可用示例音频测试 <code>play_media</code>。留空选项为自动识别（优先小爱）。</p>
    <label for="player">目标 media_player</label>
    <select id="player"></select>
    <div class="row">
      <button class="save" id="btnSave" onclick="savePlayer()">保存</button>
      <button class="start" id="btnStart" onclick="startTest()">开始测试</button>
      <button class="stop" id="btnStop" onclick="stopTest()">停止测试</button>
    </div>
    <div class="meta" id="status">加载中…</div>
    <h2>AirPlay 播放日志</h2>
    <p>iPhone 投屏时这里会实时显示钩子 / ffmpeg / play_media（同步出现在插件日志里，前缀 <code>[play]</code>）。</p>
    <div class="logbox" id="playlog">（尚无日志）</div>
  </div>
  <script>
    let lastPlayers = [];
    async function api(path, method, body) {
      const opts = { method: method || 'GET', headers: {} };
      if (body !== undefined) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
      }
      const res = await fetch(path, opts);
      const data = await res.json().catch(() => ({}));
      if (!res.ok && !data.error) data.error = 'HTTP ' + res.status;
      return data;
    }
    function fillSelect(data) {
      const sel = document.getElementById('player');
      const current = (data.selected != null ? data.selected : data.configured) || '';
      const players = data.players || lastPlayers || [];
      lastPlayers = players;
      const keep = sel.value;
      sel.innerHTML = '';
      const auto = document.createElement('option');
      auto.value = '';
      auto.textContent = '自动识别（推荐）';
      sel.appendChild(auto);
      for (const p of players) {
        const opt = document.createElement('option');
        opt.value = p.entity_id;
        const mark = (p.score || 0) >= 100 ? ' ★' : '';
        opt.textContent = (p.friendly_name || p.entity_id) + ' — ' + p.entity_id + ' [' + p.state + ']' + mark;
        sel.appendChild(opt);
      }
      const prefer = keep || current || '';
      if ([...sel.options].some(o => o.value === prefer)) sel.value = prefer;
      else sel.value = '';
    }
    function render(data) {
      fillSelect(data);
      const el = document.getElementById('status');
      const lines = [];
      if (data.error) lines.push('错误: ' + data.error);
      if (data.message) lines.push(data.message);
      lines.push('配置: ' + (data.configured || data.selected || '(自动)'));
      lines.push('实际目标: ' + (data.entity_id || data.resolved_entity_id || '-'));
      if (data.friendly_name) lines.push('名称: ' + data.friendly_name);
      if (data.airplay_name) lines.push('AirPlay: ' + data.airplay_name);
      lines.push('状态: ' + (data.testing ? '测试播放中' : '空闲'));
      if (data.stream_url) lines.push('URL: ' + data.stream_url);
      el.textContent = lines.join('\\n');
      el.className = 'meta ' + (data.error ? 'err' : 'ok');
      document.getElementById('btnStart').disabled = !!data.testing;
      document.getElementById('btnStop').disabled = !data.testing;
    }
    async function refresh() { render(await api('./api/status')); }
    async function savePlayer() {
      document.getElementById('btnSave').disabled = true;
      try {
        const entity_id = document.getElementById('player').value;
        render(await api('./api/player', 'POST', { entity_id }));
      } finally {
        document.getElementById('btnSave').disabled = false;
      }
    }
    async function startTest() {
      document.getElementById('btnStart').disabled = true;
      // Save current dropdown selection before testing.
      await api('./api/player', 'POST', { entity_id: document.getElementById('player').value });
      render(await api('./api/test/start', 'POST'));
    }
    async function stopTest() {
      document.getElementById('btnStop').disabled = true;
      render(await api('./api/test/stop', 'POST'));
    }
    async function refreshLog() {
      const data = await api('./api/play-log');
      const el = document.getElementById('playlog');
      const text = (data.text || '').trim();
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
      el.textContent = text || '（尚无日志 — 用 iPhone 选 XiaoAir 播放后会出现）';
      if (atBottom) el.scrollTop = el.scrollHeight;
    }
    refresh();
    refreshLog();
    setInterval(refresh, 5000);
    setInterval(refreshLog, 1500);
  </script>
</body>
</html>
"""


class UIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[ui] {self.address_string()} {fmt % args}", flush=True)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/index.html"):
            self._send_html(HTML)
            return
        if path.endswith("/api/status") or path == "/api/status":
            self._send_json(200, status())
            return
        if path.endswith("/api/players") or path == "/api/players":
            self._send_json(200, list_players())
            return
        if path.endswith("/api/play-log") or path == "/api/play-log":
            text = ""
            try:
                with open("/tmp/xiaoair-play.log", "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read()[-20000:]
            except OSError:
                text = ""
            self._send_json(200, {"ok": True, "text": text})
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        try:
            if path.endswith("/api/player") or path == "/api/player":
                body = self._read_json()
                result = save_media_player(str(body.get("entity_id") or ""))
                # Include player list so UI can refresh in one shot.
                result.update({k: v for k, v in list_players().items() if k != "ok"})
                result["testing"] = bool(read_state().get("testing"))
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path.endswith("/api/test/start") or path == "/api/test/start":
                result = start_test()
                result.update({k: v for k, v in list_players().items() if k != "ok"})
                self._send_json(200 if result.get("ok") else 400, result)
                return
            if path.endswith("/api/test/stop") or path == "/api/test/stop":
                result = stop_test()
                result.update({k: v for k, v in list_players().items() if k != "ok"})
                self._send_json(200 if result.get("ok") else 400, result)
                return
        except Exception as err:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": str(err)})
            return
        self.send_error(404)


def main() -> None:
    if not SUPERVISOR_TOKEN:
        print("WARNING: SUPERVISOR_TOKEN missing", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", UI_PORT), UIHandler)
    print(f"XiaoAir UI listening on :{UI_PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        stop_test()
        server.server_close()


if __name__ == "__main__":
    main()

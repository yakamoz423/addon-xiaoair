#!/usr/bin/env python3
"""
Shairport play/stop hooks.

Starts a reconnectable HTTP MP3 stream from the Shairport pipe and calls
media_player.play_media on the configured Home Assistant entity.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue
from typing import Any, Dict, List, Optional

import requests

OPTIONS_FILE = "/data/options.json"
ENV_FILE = "/tmp/xiaoair-env.json"
PLAY_LOG = "/tmp/xiaoair-play.log"
STREAM_PORT_BASE = 7000


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        with open(PLAY_LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def load_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def load_options() -> Dict[str, Any]:
    return load_json(OPTIONS_FILE)


def supervisor_token() -> Optional[str]:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        return token
    return load_json(ENV_FILE).get("SUPERVISOR_TOKEN")


def get_local_ip() -> str:
    env = load_json(ENV_FILE)
    if env.get("local_ip"):
        return str(env["local_ip"])
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        local_ip = sock.getsockname()[0]
        sock.close()
        return local_ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


def state_path(entity_id: str) -> str:
    return f"/tmp/shairport_state_{entity_id.replace('.', '_')}.json"


def pid_path(entity_id: str) -> str:
    return f"/tmp/shairport_serve_{entity_id.replace('.', '_')}.pid"


def wait_for_port(port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def kill_pid(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except Exception:  # noqa: BLE001
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:  # noqa: BLE001
            pass


def stop_serve(entity_id: str) -> None:
    path = pid_path(entity_id)
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            pid = int(handle.read().strip())
        kill_pid(pid)
    except Exception as err:  # noqa: BLE001
        log(f"stop_serve kill failed: {err}")
    try:
        os.remove(path)
    except OSError:
        pass


class Fanout:
    """Broadcast ffmpeg chunks to all connected HTTP clients."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: List[Queue] = []

    def subscribe(self) -> Queue:
        queue: Queue = Queue(maxsize=64)
        with self._lock:
            self._clients.append(queue)
        return queue

    def unsubscribe(self, queue: Queue) -> None:
        with self._lock:
            if queue in self._clients:
                self._clients.remove(queue)

    def publish(self, data: bytes) -> None:
        with self._lock:
            clients = list(self._clients)
        for queue in clients:
            try:
                queue.put_nowait(data)
            except Exception:  # noqa: BLE001
                # Drop for slow clients rather than blocking encode.
                try:
                    _ = queue.get_nowait()
                    queue.put_nowait(data)
                except Exception:  # noqa: BLE001
                    pass


def run_serve(entity_id: str, pipe_path: str, port_offset: int) -> None:
    options = load_options()
    stream_format = str(options.get("stream_format") or "mp3").lower()
    port = STREAM_PORT_BASE + port_offset
    stream_path = "/live.mp3" if stream_format == "mp3" else "/live.wav"
    content_type = "audio/mpeg" if stream_format == "mp3" else "audio/wav"
    fanout = Fanout()

    if stream_format == "wav":
        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "s16le",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-i",
            pipe_path,
            "-f",
            "wav",
            "pipe:1",
        ]
    else:
        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "s16le",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-i",
            pipe_path,
            "-f",
            "mp3",
            "-b:a",
            "192k",
            "pipe:1",
        ]

    log(f"serve start entity={entity_id} port={port} pipe={pipe_path}")
    log(f"ffmpeg: {' '.join(ffmpeg_cmd)}")

    ffmpeg = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    def pump_stderr() -> None:
        assert ffmpeg.stderr is not None
        for raw in iter(ffmpeg.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                log(f"ffmpeg: {line}")

    def pump_stdout() -> None:
        assert ffmpeg.stdout is not None
        while True:
            chunk = ffmpeg.stdout.read(4096)
            if not chunk:
                break
            fanout.publish(chunk)
        log("ffmpeg stdout ended")

    threading.Thread(target=pump_stderr, daemon=True).start()
    threading.Thread(target=pump_stdout, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            log(f"http: {self.address_string()} {fmt % args}")

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] not in (stream_path, "/"):
                self.send_error(404)
                return
            queue = fanout.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    try:
                        data = queue.get(timeout=60)
                    except Empty:
                        log("http client idle timeout")
                        break
                    self.wfile.write(data)
                    self.wfile.flush()
            except Exception as err:  # noqa: BLE001
                log(f"http client gone: {err}")
            finally:
                fanout.unsubscribe(queue)

        def do_HEAD(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log(f"HTTP listening on 0.0.0.0:{port}{stream_path}")

    def shutdown(*_args: Any) -> None:
        log("serve shutting down")
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            os.killpg(ffmpeg.pid, signal.SIGTERM)
        except Exception:  # noqa: BLE001
            try:
                ffmpeg.terminate()
            except Exception:  # noqa: BLE001
                pass

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    with open(pid_path(entity_id), "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        shutdown()
        try:
            os.remove(pid_path(entity_id))
        except OSError:
            pass


def play_media(entity_id: str, stream_url: str, media_content_type: str) -> bool:
    token = supervisor_token()
    if not token:
        log("✗ SUPERVISOR_TOKEN missing — cannot call play_media")
        return False
    payload = {
        "entity_id": entity_id,
        "media_content_id": stream_url,
        "media_content_type": media_content_type,
    }
    log(f"play_media {entity_id} <- {stream_url}")
    try:
        response = requests.post(
            "http://supervisor/core/api/services/media_player/play_media",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        log(f"✓ play_media accepted ({response.status_code})")
        return True
    except Exception as err:  # noqa: BLE001
        log(f"✗ play_media failed: {err}")
        return False


def stop_media(entity_id: str) -> bool:
    token = supervisor_token()
    if not token:
        log("✗ SUPERVISOR_TOKEN missing — cannot call media_stop")
        return False
    try:
        response = requests.post(
            "http://supervisor/core/api/services/media_player/media_stop",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"entity_id": entity_id},
            timeout=10,
        )
        response.raise_for_status()
        log(f"✓ media_stop accepted for {entity_id}")
        return True
    except Exception as err:  # noqa: BLE001
        log(f"✗ media_stop failed: {err}")
        return False


def handle_start(entity_id: str, pipe_path: str, port_offset: str) -> None:
    offset = int(port_offset)
    port = STREAM_PORT_BASE + offset
    options = load_options()
    stream_format = str(options.get("stream_format") or "mp3").lower()
    media_content_type = str(options.get("media_content_type") or "music")
    stream_name = "live.mp3" if stream_format == "mp3" else "live.wav"
    stream_url = f"http://{get_local_ip()}:{port}/{stream_name}"

    log(f"Playback starting for {entity_id}")
    log(f"pipe={pipe_path} url={stream_url} token={'yes' if supervisor_token() else 'no'}")

    stop_serve(entity_id)
    time.sleep(0.2)

    proc = subprocess.Popen(
        [
            sys.executable,
            "/usr/bin/shairport-play-handler.py",
            "serve",
            entity_id,
            pipe_path,
            str(offset),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )
    log(f"serve pid={proc.pid}")

    if not wait_for_port(port, timeout=8.0):
        log(f"✗ HTTP port {port} did not open in time")
        return

    # Give ffmpeg a moment to attach to the FIFO before XiaoAI connects.
    time.sleep(0.3)
    play_media(entity_id, stream_url, media_content_type)

    with open(state_path(entity_id), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "entity_id": entity_id,
                "pipe_path": pipe_path,
                "port": offset,
                "stream_url": stream_url,
                "serve_pid": proc.pid,
            },
            handle,
        )


def handle_stop(entity_id: str) -> None:
    log(f"Playback stopping for {entity_id}")
    path = state_path(entity_id)
    try:
        stop_serve(entity_id)
        stop_media(entity_id)
        if os.path.exists(path):
            os.remove(path)
    except Exception as err:  # noqa: BLE001
        log(f"✗ Error in stop handler: {err}")


def main() -> None:
    if len(sys.argv) < 3:
        print(
            "Usage: shairport-play-handler.py <start|stop|serve> <entity_id> [pipe_path] [port]",
            file=sys.stderr,
        )
        sys.exit(1)

    command = sys.argv[1]
    entity_id = sys.argv[2]

    if command == "start":
        if len(sys.argv) < 5:
            log("start command requires pipe_path and port")
            sys.exit(1)
        handle_start(entity_id, sys.argv[3], sys.argv[4])
    elif command == "stop":
        handle_stop(entity_id)
    elif command == "serve":
        if len(sys.argv) < 5:
            log("serve command requires pipe_path and port")
            sys.exit(1)
        run_serve(entity_id, sys.argv[3], int(sys.argv[4]))
    else:
        log(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()

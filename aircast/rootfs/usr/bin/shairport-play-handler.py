#!/usr/bin/env python3
"""
Shairport play/stop hooks.

Starts an HTTP audio stream from the Shairport pipe and calls
media_player.play_media on the configured Home Assistant entity.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import Any, Dict, Optional

import requests

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
HA_API_URL = "http://supervisor/core/api"
OPTIONS_FILE = "/data/options.json"
STREAM_PORT_BASE = 7000


def load_options() -> Dict[str, Any]:
    try:
        with open(OPTIONS_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:  # noqa: BLE001
        return {}


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


def state_path(entity_id: str) -> str:
    return f"/tmp/shairport_state_{entity_id.replace('.', '_')}.json"


class AudioStreamHandler:
    """Pipe -> ffmpeg HTTP stream -> HA media_player.play_media."""

    def __init__(self, entity_id: str, pipe_path: str, port_offset: int):
        self.entity_id = entity_id
        self.pipe_path = pipe_path
        self.port_offset = port_offset
        self.port = STREAM_PORT_BASE + port_offset
        self.options = load_options()
        self.stream_format = str(self.options.get("stream_format") or "mp3").lower()
        self.media_content_type = str(
            self.options.get("media_content_type") or "music"
        )
        self.ffmpeg_process: Optional[subprocess.Popen] = None

    @property
    def stream_path(self) -> str:
        return "live.mp3" if self.stream_format == "mp3" else "live.wav"

    @property
    def stream_url(self) -> str:
        return f"http://{get_local_ip()}:{self.port}/{self.stream_path}"

    def start_stream(self) -> None:
        if self.stream_format == "wav":
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
                self.pipe_path,
                "-f",
                "wav",
                "-content_type",
                "audio/wav",
                "-listen",
                "1",
                f"http://0.0.0.0:{self.port}/{self.stream_path}",
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
                self.pipe_path,
                "-f",
                "mp3",
                "-content_type",
                "audio/mpeg",
                "-listen",
                "1",
                f"http://0.0.0.0:{self.port}/{self.stream_path}",
            ]

        print(f"Starting FFmpeg stream: {' '.join(ffmpeg_cmd)}", file=sys.stderr)
        self.ffmpeg_process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        time.sleep(0.5)

    def stop_stream(self) -> None:
        if not self.ffmpeg_process:
            return
        try:
            os.killpg(self.ffmpeg_process.pid, signal.SIGTERM)
        except Exception:  # noqa: BLE001
            try:
                self.ffmpeg_process.terminate()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.ffmpeg_process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                os.killpg(self.ffmpeg_process.pid, signal.SIGKILL)
            except Exception:  # noqa: BLE001
                pass

    def play_media(self) -> bool:
        payload = {
            "entity_id": self.entity_id,
            "media_content_id": self.stream_url,
            "media_content_type": self.media_content_type,
        }
        print(
            f"Calling media_player.play_media on {self.entity_id}: {self.stream_url}",
            file=sys.stderr,
        )
        try:
            response = requests.post(
                f"{HA_API_URL}/services/media_player/play_media",
                headers={
                    "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            print(f"✓ play_media accepted for {self.entity_id}", file=sys.stderr)
            return True
        except Exception as err:  # noqa: BLE001
            print(f"✗ play_media failed for {self.entity_id}: {err}", file=sys.stderr)
            return False

    def stop_media(self) -> bool:
        try:
            response = requests.post(
                f"{HA_API_URL}/services/media_player/media_stop",
                headers={
                    "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={"entity_id": self.entity_id},
                timeout=10,
            )
            response.raise_for_status()
            print(f"✓ media_stop accepted for {self.entity_id}", file=sys.stderr)
            return True
        except Exception as err:  # noqa: BLE001
            print(f"✗ media_stop failed for {self.entity_id}: {err}", file=sys.stderr)
            return False


def handle_start(entity_id: str, pipe_path: str, port_offset: str) -> None:
    print(f"Playback starting for {entity_id}", file=sys.stderr)
    handler = AudioStreamHandler(entity_id, pipe_path, int(port_offset))
    handler.start_stream()
    handler.play_media()

    with open(state_path(entity_id), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "entity_id": entity_id,
                "pipe_path": pipe_path,
                "port": handler.port_offset,
                "stream_url": handler.stream_url,
                "ffmpeg_pid": handler.ffmpeg_process.pid if handler.ffmpeg_process else None,
            },
            handle,
        )


def handle_stop(entity_id: str) -> None:
    print(f"Playback stopping for {entity_id}", file=sys.stderr)
    path = state_path(entity_id)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)

        handler = AudioStreamHandler(
            entity_id,
            state.get("pipe_path", ""),
            int(state.get("port", 0)),
        )
        ffmpeg_pid = state.get("ffmpeg_pid")
        if ffmpeg_pid:
            try:
                os.killpg(int(ffmpeg_pid), signal.SIGTERM)
            except Exception:  # noqa: BLE001
                try:
                    os.kill(int(ffmpeg_pid), signal.SIGTERM)
                except Exception:  # noqa: BLE001
                    pass

        handler.stop_media()
        if os.path.exists(path):
            os.remove(path)
    except Exception as err:  # noqa: BLE001
        print(f"✗ Error in stop handler: {err}", file=sys.stderr)


def main() -> None:
    if len(sys.argv) < 3:
        print(
            "Usage: shairport-play-handler.py <start|stop> <entity_id> [pipe_path] [port]",
            file=sys.stderr,
        )
        sys.exit(1)

    command = sys.argv[1]
    entity_id = sys.argv[2]

    if command == "start":
        if len(sys.argv) < 5:
            print("start command requires pipe_path and port", file=sys.stderr)
            sys.exit(1)
        handle_start(entity_id, sys.argv[3], sys.argv[4])
    elif command == "stop":
        handle_stop(entity_id)
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

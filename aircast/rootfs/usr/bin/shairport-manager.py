#!/usr/bin/env python3
"""Manage Shairport-Sync instances for configured HA media_player entities."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
SHAIRPORT_CONFIG_DIR = "/tmp/shairport-configs"
OPTIONS_FILE = "/data/options.json"
MAX_BACKOFF_SEC = 60


class ShairportSyncManager:
    """Creates and monitors one Shairport-Sync instance per media_player."""

    def __init__(self, players: List[Dict[str, Any]], config: Dict[str, Any]):
        self.players = players
        self.config = config
        self.processes: Dict[str, subprocess.Popen] = {}
        self.stream_pipes: Dict[str, str] = {}
        self.log_files: Dict[str, str] = {}
        self.log_handles: Dict[str, Any] = {}
        self.fail_counts: Dict[str, int] = {}
        self.next_restart_at: Dict[str, float] = {}
        Path(SHAIRPORT_CONFIG_DIR).mkdir(exist_ok=True)

    def create_shairport_config(self, player: Dict[str, Any], port_offset: int) -> str:
        player_name = player["friendly_name"].replace("\\", "\\\\").replace('"', '\\"')
        entity_id = player["entity_id"]
        entity_key = entity_id.replace(".", "_")
        config_path = f"{SHAIRPORT_CONFIG_DIR}/{entity_key}.conf"
        pipe_path = f"/tmp/shairport_{entity_key}.pipe"
        # Shairport runs these via `sh -c`, so shell redirects work.
        # Hook stdout/stderr are NOT shown in the add-on log otherwise.
        start_cmd = (
            f"/usr/bin/python3 /usr/bin/shairport-play-handler.py "
            f"start {entity_id} {pipe_path} {port_offset} "
            f">>/tmp/xiaoair-play.log 2>&1"
        )
        stop_cmd = (
            f"/usr/bin/python3 /usr/bin/shairport-play-handler.py "
            f"stop {entity_id} >>/tmp/xiaoair-play.log 2>&1"
        )

        if os.path.exists(pipe_path):
            try:
                os.remove(pipe_path)
            except OSError as err:
                print(f"Warning: could not remove old pipe {pipe_path}: {err}")
        if not os.path.exists(pipe_path):
            os.mkfifo(pipe_path)
        self.stream_pipes[entity_id] = pipe_path

        # tinysvcmdns: works without host Avahi (HAOS usually has no Avahi on D-Bus).
        mdns_backend = os.environ.get("XIAOAIR_MDNS_BACKEND", "tinysvcmdns")

        config_content = f"""
general = {{
    name = "{player_name}";
    output_backend = "pipe";
    mdns_backend = "{mdns_backend}";
    port = {5000 + port_offset};
    interpolation = "soxr";
}};

sessioncontrol = {{
    session_timeout = 20;
    allow_session_interruption = "yes";
    run_this_before_play_begins = "{start_cmd}";
    run_this_after_play_ends = "{stop_cmd}";
    // Wait until HTTP stream + play_media are ready before audio hits the pipe.
    wait_for_completion = "yes";
}};

pipe = {{
    name = "{pipe_path}";
}};

metadata = {{
    enabled = "yes";
    include_cover_art = "no";
}};
"""
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write(config_content)
        return config_path

    def _drain_log(self, entity_id: str, process: subprocess.Popen) -> str:
        log_path = self.log_files.get(entity_id)
        chunks: List[str] = []
        if process.stdout:
            try:
                process.stdout.flush()
            except Exception:  # noqa: BLE001
                pass
        if log_path and os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                    chunks.append(handle.read()[-4000:])
            except OSError:
                pass
        return "\n".join(chunks).strip()

    def start_shairport_instance(
        self, player: Dict[str, Any], port_offset: int
    ) -> subprocess.Popen:
        config_path = self.create_shairport_config(player, port_offset)
        entity_id = player["entity_id"]
        log_path = f"/tmp/shairport_{entity_id.replace('.', '_')}.log"
        self.log_files[entity_id] = log_path
        old_handle = self.log_handles.pop(entity_id, None)
        if old_handle is not None:
            try:
                old_handle.close()
            except Exception:  # noqa: BLE001
                pass
        log_handle = open(log_path, "ab", buffering=0)
        self.log_handles[entity_id] = log_handle
        cmd = ["shairport-sync", "-c", config_path, "-v"]
        print(
            f"Starting Shairport-Sync for {player['friendly_name']} -> {entity_id}"
        )
        print(f"Command: {' '.join(cmd)}")
        print(f"Log file: {log_path}")
        return subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def start_all(self) -> None:
        for idx, player in enumerate(self.players):
            try:
                process = self.start_shairport_instance(player, idx)
                self.processes[player["entity_id"]] = process
                # Give it a moment so immediate avahi/config failures show up.
                time.sleep(0.8)
                if process.poll() is not None:
                    detail = self._drain_log(player["entity_id"], process)
                    print(
                        f"✗ Shairport-Sync exited immediately "
                        f"(code={process.returncode}) for {player['entity_id']}"
                    )
                    if detail:
                        print("--- shairport-sync output ---")
                        print(detail)
                        print("--- end ---")
                    else:
                        print(
                            "(no output captured — usually Avahi/D-Bus missing; "
                            "ensure host_dbus / avahi-daemon)"
                        )
                else:
                    print(
                        f"✓ AirPlay '{player['friendly_name']}' -> {player['entity_id']}"
                    )
            except Exception as err:  # noqa: BLE001
                print(
                    f"✗ Failed to start Shairport-Sync for {player['entity_id']}: {err}"
                )

    def stop_all(self) -> None:
        print("\nStopping all Shairport-Sync instances...")
        for entity_id, process in self.processes.items():
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
                print(f"✓ Stopped {entity_id}")
            except Exception as err:  # noqa: BLE001
                print(f"✗ Error stopping {entity_id}: {err}")
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    pass

        for pipe_path in self.stream_pipes.values():
            try:
                if os.path.exists(pipe_path):
                    os.remove(pipe_path)
            except Exception:  # noqa: BLE001
                pass

    def _backoff_seconds(self, entity_id: str) -> int:
        fails = self.fail_counts.get(entity_id, 0)
        return min(MAX_BACKOFF_SEC, 5 * (2 ** min(fails, 3)))

    def _follow_text_file(self, path: str, offset: int, prefix: str) -> int:
        """Print new lines from path to add-on logs; return updated byte offset."""
        if not os.path.exists(path):
            return offset
        try:
            size = os.path.getsize(path)
            if offset > size:
                offset = 0
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                data = handle.read()
                offset = handle.tell()
            if data:
                for line in data.splitlines():
                    if line.strip():
                        print(f"{prefix}{line}", flush=True)
        except OSError:
            pass
        return offset

    def monitor(self) -> None:
        play_log_offset = 0
        # Truncate stale play log so restart starts clean in HA logs.
        try:
            open("/tmp/xiaoair-play.log", "w", encoding="utf-8").close()
        except OSError:
            pass
        print("Tailing AirPlay play hooks into add-on logs ([play] ...)", flush=True)

        while True:
            play_log_offset = self._follow_text_file(
                "/tmp/xiaoair-play.log", play_log_offset, "[play] "
            )

            now = time.time()
            for entity_id, process in list(self.processes.items()):
                code = process.poll()
                if code is None:
                    continue

                due = self.next_restart_at.get(entity_id, 0)
                if now < due:
                    continue

                detail = self._drain_log(entity_id, process)
                self.fail_counts[entity_id] = self.fail_counts.get(entity_id, 0) + 1
                wait_for = self._backoff_seconds(entity_id)

                print(
                    f"⚠ Shairport-Sync for {entity_id} died "
                    f"(code={code}, fails={self.fail_counts[entity_id]}), "
                    f"restarting in {wait_for}s..."
                )
                if detail:
                    print("--- shairport-sync output ---")
                    print(detail)
                    print("--- end ---")

                self.next_restart_at[entity_id] = now + wait_for
                time.sleep(wait_for)

                player = next(p for p in self.players if p["entity_id"] == entity_id)
                idx = self.players.index(player)
                try:
                    new_proc = self.start_shairport_instance(player, idx)
                    self.processes[entity_id] = new_proc
                    time.sleep(0.8)
                    if new_proc.poll() is None:
                        print(f"✓ Restarted Shairport-Sync for {entity_id}")
                        self.fail_counts[entity_id] = 0
                        self.next_restart_at[entity_id] = 0
                    else:
                        print(
                            f"✗ Restart still failing for {entity_id} "
                            f"(code={new_proc.returncode})"
                        )
                        more = self._drain_log(entity_id, new_proc)
                        if more:
                            print(more[-2000:])
                        self.next_restart_at[entity_id] = (
                            time.time() + self._backoff_seconds(entity_id)
                        )
                except Exception as err:  # noqa: BLE001
                    print(f"✗ Failed to restart: {err}")
                    self.next_restart_at[entity_id] = (
                        time.time() + self._backoff_seconds(entity_id)
                    )
            time.sleep(0.5)


def load_config() -> Dict[str, Any]:
    try:
        with open(OPTIONS_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:  # noqa: BLE001
        return {}


def load_players() -> List[Dict[str, Any]]:
    try:
        result = subprocess.run(
            ["/usr/bin/media-players.py"],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = [line for line in result.stdout.strip().split("\n") if line.strip()]
        if not lines:
            return []
        return json.loads(lines[-1])
    except Exception as err:  # noqa: BLE001
        print(f"Error loading configured media players: {err}", file=sys.stderr)
        if "result" in locals():
            print(result.stderr, file=sys.stderr)
        return []


def _avahi_on_dbus() -> tuple[bool, str]:
    check = [
        "dbus-send",
        "--system",
        "--print-reply",
        "--dest=org.freedesktop.Avahi",
        "/",
        "org.freedesktop.Avahi.Server.GetVersion",
    ]
    try:
        proc = subprocess.run(check, capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            return True, (proc.stdout or "").strip()
        return False, (proc.stderr or proc.stdout or "").strip()
    except Exception as err:  # noqa: BLE001
        return False, str(err)


def ensure_mdns() -> None:
    """Avahi is optional; default mdns backend is tinysvcmdns."""
    backend = os.environ.get("XIAOAIR_MDNS_BACKEND", "tinysvcmdns")
    print(f"mDNS backend: {backend}")
    if backend != "avahi":
        return

    ok, detail = _avahi_on_dbus()
    if ok:
        print("✓ Avahi available on system D-Bus")
        return
    print(f"Avahi not on D-Bus yet: {detail}")
    print("Hint: use tinysvcmdns (default) if host Avahi is unavailable")


def detect_local_ip() -> str:
    try:
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        local_ip = sock.getsockname()[0]
        sock.close()
        return local_ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


def write_runtime_env() -> None:
    """Persist token/IP for Shairport hooks (env may not always be inherited)."""
    payload = {
        "SUPERVISOR_TOKEN": SUPERVISOR_TOKEN,
        "local_ip": detect_local_ip(),
    }
    with open("/tmp/xiaoair-env.json", "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    print(f"Runtime env written (local_ip={payload['local_ip']})")


def main() -> None:
    print("=" * 60)
    print("Starting XiaoAir media_player bridge manager...")
    print("=" * 60)

    if not SUPERVISOR_TOKEN:
        print("ERROR: SUPERVISOR_TOKEN not found in environment")
        sys.exit(1)

    write_runtime_env()
    ensure_mdns()

    config = load_config()
    print(f"✓ Configuration loaded: {json.dumps(config, ensure_ascii=False)}")

    if not config.get("media_bridge_enabled", True):
        print("Media bridge is disabled in configuration")
        sys.exit(0)

    print("\nLoading configured media_player targets...")
    players = load_players()

    if not players:
        print("⚠ No media_player entities configured")
        print("Add players in the add-on Configuration tab, then restart.")
        while True:
            time.sleep(60)
            print("Rechecking configured media_player targets...")
            players = load_players()
            if players:
                print(f"\n✓ Found {len(players)} configured player(s)!")
                break

    print(f"\nUsing {len(players)} media_player target(s)")
    for player in players:
        print(f"  • {player['friendly_name']} ({player['entity_id']})")

    manager = ShairportSyncManager(players, config)

    def signal_handler(signum, frame):  # noqa: ANN001, ARG001
        print("\nReceived shutdown signal")
        manager.stop_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    manager.start_all()
    print(f"\n{'=' * 60}")
    print("AirPlay receivers are running. Select them on iPhone/Mac.")
    print(f"{'=' * 60}\n")

    try:
        manager.monitor()
    except KeyboardInterrupt:
        manager.stop_all()


if __name__ == "__main__":
    main()

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
        start_cmd = (
            f"/usr/bin/python3 /usr/bin/shairport-play-handler.py "
            f"start {entity_id} {pipe_path} {port_offset}"
        )
        stop_cmd = (
            f"/usr/bin/python3 /usr/bin/shairport-play-handler.py stop {entity_id}"
        )

        if os.path.exists(pipe_path):
            try:
                os.remove(pipe_path)
            except OSError as err:
                print(f"Warning: could not remove old pipe {pipe_path}: {err}")
        if not os.path.exists(pipe_path):
            os.mkfifo(pipe_path)
        self.stream_pipes[entity_id] = pipe_path

        # Prefer Avahi (host_dbus on HAOS). tinysvcmdns only if built in.
        mdns_backend = os.environ.get("XIAOAIR_MDNS_BACKEND", "avahi")

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
    wait_for_completion = "no";
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

    def monitor(self) -> None:
        while True:
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
            time.sleep(2)


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


def ensure_avahi() -> None:
    """Use existing system D-Bus (host_dbus); start only avahi-daemon if needed."""
    ok, detail = _avahi_on_dbus()
    if ok:
        print("✓ Avahi available on system D-Bus")
        return
    print(f"Avahi not on D-Bus yet: {detail}")

    # With host_dbus, /run/dbus/system_bus_socket already exists. Starting a
    # second dbus-daemon fails with "Address already in use". Only start Avahi.
    os.makedirs("/var/run/avahi-daemon", exist_ok=True)
    print("Starting avahi-daemon on existing system D-Bus...")
    try:
        subprocess.run(
            ["avahi-daemon", "-c"],
            check=False,
            capture_output=True,
            timeout=5,
        )
        # -k kills if already running; ignore failure
        subprocess.run(
            ["avahi-daemon", "-k"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:  # noqa: BLE001
        pass

    try:
        proc = subprocess.run(
            ["avahi-daemon", "--daemonize", "--no-drop-root", "--no-rlimits"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            print(
                f"Warning: avahi-daemon start rc={proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()}"
            )
    except Exception as err:  # noqa: BLE001
        print(f"Warning: avahi-daemon start failed: {err}")

    time.sleep(1.5)
    ok, detail = _avahi_on_dbus()
    if ok:
        print("✓ Avahi started on system D-Bus")
    else:
        print(
            "✗ Avahi still unavailable — Shairport-Sync may exit. "
            f"{detail}"
        )


def main() -> None:
    print("=" * 60)
    print("Starting XiaoAir media_player bridge manager...")
    print("=" * 60)

    if not SUPERVISOR_TOKEN:
        print("ERROR: SUPERVISOR_TOKEN not found in environment")
        sys.exit(1)

    ensure_avahi()

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

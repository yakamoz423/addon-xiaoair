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
from typing import Any, Dict, List

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
SHAIRPORT_CONFIG_DIR = "/tmp/shairport-configs"
OPTIONS_FILE = "/data/options.json"


class ShairportSyncManager:
    """Creates and monitors one Shairport-Sync instance per media_player."""

    def __init__(self, players: List[Dict[str, Any]], config: Dict[str, Any]):
        self.players = players
        self.config = config
        self.processes: Dict[str, subprocess.Popen] = {}
        self.stream_pipes: Dict[str, str] = {}
        Path(SHAIRPORT_CONFIG_DIR).mkdir(exist_ok=True)

    def create_shairport_config(self, player: Dict[str, Any], port_offset: int) -> str:
        player_name = player["friendly_name"].replace('"', '\\"')
        entity_key = player["entity_id"].replace(".", "_")
        config_path = f"{SHAIRPORT_CONFIG_DIR}/{entity_key}.conf"
        pipe_path = f"/tmp/shairport_{entity_key}.pipe"

        if os.path.exists(pipe_path):
            os.remove(pipe_path)
        os.mkfifo(pipe_path)
        self.stream_pipes[player["entity_id"]] = pipe_path

        config_content = f"""
general = {{
    name = "{player_name}";
    output_backend = "pipe";
    mdns_backend = "avahi";
    port = {5000 + port_offset};
    interpolation = "soxr";
}};

sessioncontrol = {{
    session_timeout = 20;
    allow_session_interruption = "yes";
    run_this_before_play_begins = "/usr/bin/python3 /usr/bin/shairport-play-handler.py start {player['entity_id']} {pipe_path} {port_offset}";
    run_this_after_play_ends = "/usr/bin/python3 /usr/bin/shairport-play-handler.py stop {player['entity_id']}";
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

    def start_shairport_instance(
        self, player: Dict[str, Any], port_offset: int
    ) -> subprocess.Popen:
        config_path = self.create_shairport_config(player, port_offset)
        cmd = ["shairport-sync", "-c", config_path, "-v"]
        print(f"Starting Shairport-Sync for {player['friendly_name']} -> {player['entity_id']}")
        print(f"Command: {' '.join(cmd)}")
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def start_all(self) -> None:
        for idx, player in enumerate(self.players):
            try:
                process = self.start_shairport_instance(player, idx)
                self.processes[player["entity_id"]] = process
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
                process.terminate()
                process.wait(timeout=5)
                print(f"✓ Stopped {entity_id}")
            except Exception as err:  # noqa: BLE001
                print(f"✗ Error stopping {entity_id}: {err}")
                try:
                    process.kill()
                except Exception:  # noqa: BLE001
                    pass

        for pipe_path in self.stream_pipes.values():
            try:
                if os.path.exists(pipe_path):
                    os.remove(pipe_path)
            except Exception:  # noqa: BLE001
                pass

    def monitor(self) -> None:
        while True:
            for entity_id, process in list(self.processes.items()):
                if process.poll() is not None:
                    print(f"⚠ Shairport-Sync for {entity_id} died, restarting...")
                    player = next(p for p in self.players if p["entity_id"] == entity_id)
                    idx = self.players.index(player)
                    try:
                        self.processes[entity_id] = self.start_shairport_instance(
                            player, idx
                        )
                    except Exception as err:  # noqa: BLE001
                        print(f"✗ Failed to restart: {err}")
            time.sleep(5)


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


def main() -> None:
    print("=" * 60)
    print("Starting XiaoAir media_player bridge manager...")
    print("=" * 60)

    if not SUPERVISOR_TOKEN:
        print("ERROR: SUPERVISOR_TOKEN not found in environment")
        sys.exit(1)

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

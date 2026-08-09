#!/usr/bin/env python3
"""Resolve configured media_player entities for XiaoAir bridge."""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

import requests

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
HA_API_URL = "http://supervisor/core/api"
OPTIONS_FILE = "/data/options.json"


def get_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }


def load_options() -> Dict[str, Any]:
    try:
        with open(OPTIONS_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as err:  # noqa: BLE001
        print(f"Warning: failed to read {OPTIONS_FILE}: {err}", file=sys.stderr)
        return {}


def fetch_state(entity_id: str) -> Optional[Dict[str, Any]]:
    try:
        response = requests.get(
            f"{HA_API_URL}/states/{entity_id}",
            headers=get_headers(),
            timeout=10,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except requests.RequestException as err:
        print(f"Warning: failed to fetch state for {entity_id}: {err}", file=sys.stderr)
        return None


def resolve_players(options: Dict[str, Any]) -> List[Dict[str, Any]]:
    configured = options.get("players") or []
    players: List[Dict[str, Any]] = []

    for index, entry in enumerate(configured):
        if isinstance(entry, str):
            entity_id = entry.strip()
            airplay_name = ""
        elif isinstance(entry, dict):
            entity_id = str(entry.get("entity_id") or "").strip()
            airplay_name = str(entry.get("airplay_name") or "").strip()
        else:
            continue

        if not entity_id:
            continue
        if not entity_id.startswith("media_player."):
            print(
                f"Warning: skipping non media_player entity: {entity_id}",
                file=sys.stderr,
            )
            continue

        state = fetch_state(entity_id)
        attributes = state.get("attributes", {}) if state else {}
        friendly_name = attributes.get("friendly_name") or entity_id
        name = airplay_name or friendly_name

        players.append(
            {
                "entity_id": entity_id,
                "friendly_name": name,
                "ha_friendly_name": friendly_name,
                "state": state.get("state") if state else "unknown",
                "index": index,
            }
        )

    return players


def main() -> None:
    if not SUPERVISOR_TOKEN:
        print("ERROR: SUPERVISOR_TOKEN not available", file=sys.stderr)
        print(json.dumps([]))
        sys.exit(0)

    options = load_options()
    players = resolve_players(options)

    if not players:
        print("No media_player entities configured", file=sys.stderr)
        print(json.dumps([]))
        sys.exit(0)

    print(f"Configured {len(players)} media_player target(s):", file=sys.stderr)
    for player in players:
        print(
            f"  - AirPlay '{player['friendly_name']}' -> {player['entity_id']}",
            file=sys.stderr,
        )

    print(json.dumps(players))


if __name__ == "__main__":
    main()

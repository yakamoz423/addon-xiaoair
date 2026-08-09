#!/usr/bin/env python3
"""Resolve the configured media_player entity for XiaoAir bridge."""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import requests

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
HA_API_URL = "http://supervisor/core/api"
OPTIONS_FILE = "/data/options.json"

# media_player.SUPPORT_PLAY_MEDIA / MediaPlayerEntityFeature.PLAY_MEDIA
SUPPORT_PLAY_MEDIA = 16384

AUTO_SENTINELS = {
    "",
    "auto",
    "media_player.change_me",
    "change_me",
}

XIAOAI_HINTS = (
    "xiaoai",
    "xiaomi",
    "xiaomusic",
    "wifispeaker",
    "l05c",
    "l06a",
    "lx06",
    "oh2p",
    "小爱",
    "小米",
)


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


def fetch_all_states() -> List[Dict[str, Any]]:
    try:
        response = requests.get(
            f"{HA_API_URL}/states",
            headers=get_headers(),
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []
    except requests.RequestException as err:
        print(f"Warning: failed to fetch HA states: {err}", file=sys.stderr)
        return []


def _text_blob(entity_id: str, attributes: Dict[str, Any]) -> str:
    parts = [
        entity_id,
        str(attributes.get("friendly_name") or ""),
        str(attributes.get("model") or ""),
        str(attributes.get("model_name") or ""),
        str(attributes.get("device_class") or ""),
        str(attributes.get("source") or ""),
    ]
    return " ".join(parts).lower()


def score_media_player(state: Dict[str, Any]) -> Tuple[int, str]:
    """Higher score = better auto-pick candidate."""
    entity_id = str(state.get("entity_id") or "")
    attributes = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    ha_state = str(state.get("state") or "unknown")
    blob = _text_blob(entity_id, attributes)
    score = 0

    if any(hint in blob for hint in XIAOAI_HINTS):
        score += 100

    supported = attributes.get("supported_features")
    try:
        if supported is not None and int(supported) & SUPPORT_PLAY_MEDIA:
            score += 20
    except (TypeError, ValueError):
        pass

    if ha_state not in {"unavailable", "unknown"}:
        score += 10
    if ha_state in {"idle", "paused", "playing", "on", "off", "standby"}:
        score += 5

    # Prefer real speakers over browser/cast helpers when no XiaoAI hint matched.
    if re.search(r"(browser|cast|chromecast|tv|web_browser|youtube)", blob):
        score -= 30

    return score, entity_id


def discover_media_players() -> List[Dict[str, Any]]:
    states = [
        state
        for state in fetch_all_states()
        if isinstance(state, dict)
        and str(state.get("entity_id") or "").startswith("media_player.")
    ]
    ranked = sorted(states, key=score_media_player, reverse=True)
    print(f"Discovered {len(ranked)} media_player entit(y/ies):", file=sys.stderr)
    for state in ranked[:12]:
        entity_id = state.get("entity_id")
        attributes = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
        friendly = attributes.get("friendly_name") or entity_id
        score, _ = score_media_player(state)
        print(
            f"  [{score:>4}] {entity_id} ({friendly}) state={state.get('state')}",
            file=sys.stderr,
        )
    return ranked


def auto_pick_media_player() -> Optional[str]:
    ranked = discover_media_players()
    if not ranked:
        return None
    best = ranked[0]
    best_score, entity_id = score_media_player(best)
    if best_score < 0:
        return None
    print(
        f"Auto-selected media_player: {entity_id} (score={best_score})",
        file=sys.stderr,
    )
    return entity_id


def configured_entity_id(options: Dict[str, Any]) -> str:
    entity_id = str(options.get("media_player") or "").strip()

    legacy = options.get("players") or []
    if (not entity_id or entity_id.lower() in AUTO_SENTINELS) and legacy:
        first = legacy[0]
        if isinstance(first, str):
            entity_id = first.strip()
        elif isinstance(first, dict):
            entity_id = str(first.get("entity_id") or "").strip()

    return entity_id


def resolve_players(options: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build player list from flat config, with auto-detect fallback."""
    players: List[Dict[str, Any]] = []

    entity_id = configured_entity_id(options)
    airplay_name = str(options.get("airplay_name") or "").strip()

    # Legacy nested config may also carry airplay_name
    legacy = options.get("players") or []
    if legacy and isinstance(legacy[0], dict) and not airplay_name:
        airplay_name = str(legacy[0].get("airplay_name") or "").strip()

    if entity_id.lower() in AUTO_SENTINELS:
        print(
            "media_player is empty/auto — discovering Home Assistant media players...",
            file=sys.stderr,
        )
        entity_id = auto_pick_media_player() or ""

    if not entity_id:
        return []

    if not entity_id.startswith("media_player."):
        print(f"Warning: not a media_player entity: {entity_id}", file=sys.stderr)
        return []

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
            "index": 0,
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
        print(
            "No media_player entity configured or auto-detected. "
            "Set Target media player to a media_player.* entity id.",
            file=sys.stderr,
        )
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

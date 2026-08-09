# XiaoAir

AirPlay bridge for Home Assistant `media_player` entities (for example Xiaomi
XiaoAI). Also keeps the original Chromecast AirCast path from AirConnect.

## Pipeline

```
AirPlay → Shairport-Sync → FFmpeg HTTP stream → media_player.play_media → entity
```

## Configuration

```yaml
log_level: info
media_bridge_enabled: true
media_player: media_player.xiaoai_l05c
airplay_name: XiaoAI AirPlay
media_content_type: music
stream_format: mp3
```

### Option: `media_bridge_enabled`

Enable/disable the Shairport → `media_player` bridge.

### Option: `media_player`

Optional Home Assistant `media_player.*` entity id. Leave empty (or `auto`) to
auto-pick at startup; XiaoAI / Xiaomi speakers are preferred. HA App config UI
cannot render a live entity dropdown (schema has no `entity(...)` type).

### Option: `airplay_name`

Name shown in the Apple AirPlay picker.

### Option: `media_content_type`

Passed to `media_player.play_media` (default `music`).

### Option: `stream_format`

- `mp3` — recommended for XiaoAI
- `wav` — alternative for players that prefer WAV

### Chromecast options

`address`, `latency_rtp`, `latency_http`, `drift`, `log_level` behave like the
upstream AirCast add-on.

## Troubleshooting

1. Confirm the entity works with Developer Tools →
   `media_player.play_media` and a known-good HTTP URL.
2. Check add-on logs for the generated `http://<lan-ip>:700x/live.mp3` URL.
3. Ensure phone, HA host, and speaker are on the same LAN.

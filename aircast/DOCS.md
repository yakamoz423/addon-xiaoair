# XiaoAir

AirPlay bridge for Home Assistant `media_player` entities (for example Xiaomi
XiaoAI). Also keeps the original Chromecast AirCast path from AirConnect.

## Pipeline

```
AirPlay → Shairport-Sync → FFmpeg HTTP stream → media_player.play_media → entity
```

## Test UI

Configuration schema cannot host entity dropdowns or action buttons. Use the
add-on **Ingress Web UI** instead: pick a `media_player` from the dropdown
(or leave Auto), **Save**, then **Start/Stop test** for a short MP3 beep via
`media_player.play_media` / `media_stop`.

## Configuration

```yaml
log_level: info
media_bridge_enabled: true
# media_player: media_player.xiaoai_l05c   # optional; empty = auto
airplay_name: XiaoAI AirPlay
media_content_type: music
stream_format: mp3
latency_offset_seconds: -2.0
backend_buffer_seconds: 0.05
silent_lead_in_seconds: 0.0
audio_ready_bytes: 1024
http_preroll_bytes: 4096
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
- `wav` — alternative; may be slightly lower encode latency

### Option: `latency_offset_seconds`

Passed **directly** to Shairport Sync as
`audio_backend_latency_offset_in_seconds`.

iPhone AirPlay typically negotiates about **2 seconds** of latency. This add-on
cannot rewrite that negotiation, but a negative offset cancels its effect so
PCM is emitted to the pipe ASAP. Default **`-2.0`** targets that typical iOS
delay.

- Stuttering / dropouts → raise toward `0` (try `-1.5`, `-1.0`)
- Still feels late → try `-2.25`

### Option: `backend_buffer_seconds`

Shairport pipe buffer length. Smaller is lower latency; too small may underrun.

### Option: `silent_lead_in_seconds`

Silence padded before playback. Use `0` for minimum start delay.

### Option: `audio_ready_bytes`

Call `play_media` after this many encoded stream bytes (lower starts sooner).

### Option: `http_preroll_bytes`

Max preroll kept for new HTTP clients joining the live stream.

### Chromecast options

`address`, `latency_rtp`, `latency_http`, `drift`, `log_level` behave like the
upstream AirCast add-on and do **not** affect the media_player bridge latency
settings above.

## Latency notes

- Phone-negotiated AirPlay latency (~2s) is honored by Shairport’s timeline.
- `latency_offset_seconds` shifts when PCM is handed to the backend relative to
  that timeline (default `-2` ≈ cancel the wait).
- Extra delay still comes from ffmpeg encode, HTTP, and XiaoAI/`play_media`
  buffering, which are largely outside this add-on’s control.

## Troubleshooting

1. Confirm the entity works with Developer Tools →
   `media_player.play_media` and a known-good HTTP URL.
2. Check add-on logs for the generated `http://<lan-ip>:700x/live.mp3` URL and
   `latency_offset_seconds=... → Shairport ...`.
3. Ensure phone, HA host, and speaker are on the same LAN.
4. If AirPlay audio stutters after lowering latency, increase
   `latency_offset_seconds` (less negative) or `backend_buffer_seconds`.

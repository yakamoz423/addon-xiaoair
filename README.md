# XiaoAir — AirPlay to Home Assistant `media_player`

Fork of [addon-aircastESP](https://github.com/DeveshwarH1996/addon-aircastESP) /
[app-aircast](https://github.com/hassio-addons/app-aircast), modified so you can
pick any Home Assistant `media_player` (e.g. Xiaomi XiaoAI via MIOT) in the
add-on UI and receive AirPlay on it.

## How it works

```
iPhone/Mac (AirPlay)
    ↓
Shairport-Sync
    ↓
FFmpeg (PCM → mp3/wav HTTP stream)
    ↓
Home Assistant API: media_player.play_media
    ↓
Configured media_player entity (XiaoAI, etc.)
```

Same shape as Developer Tools → Actions → `media_player.play_media`.

## Install

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add: `https://github.com/yakamoz423/addon-xiaoair`
3. Install **XiaoAir**
4. Configure (see below) and start

First build compiles Shairport-Sync and may take a while.

## Configuration

```yaml
media_bridge_enabled: true
media_player: media_player.your_xiaoai
airplay_name: Living Room XiaoAI
media_content_type: music
stream_format: mp3
```

In the UI:

- **Target media player**: entity picker (same as Developer Tools → `media_player.play_media`)
- **AirPlay name**: name advertised to Apple devices
- **media_content_type**: usually `music`
- **stream_format**: `mp3` recommended for XiaoAI

Chromecast / original AirCast options (`latency_*`, `drift`, `address`) remain available.

## Usage

1. Set at least one `media_player` under **players**
2. Start the add-on
3. On iPhone/Mac, pick the AirPlay target named by `airplay_name` / friendly name
4. Check add-on logs for `play_media` / stream URL

## Notes

- Target player must be able to pull a local HTTP audio URL (you already verified XiaoAI can)
- Use the Home Assistant host LAN IP (add-on uses `host_network`)
- `ffmpeg -listen` serves one client; reconnects may restart the stream process
- Latency of several seconds is expected

## License

MIT — original AirCast by Franck Nijhof; ESPHome bridge by DeveshwarH1996;
XiaoAir media_player targeting adaptations in this fork.

# Now Playing for Spotify — a herdr plugin

See and control what's playing on Spotify without leaving [herdr](https://herdr.dev):

- **Player pane**: track, artist, album, progress, volume, shuffle/repeat, with keyboard controls
- **Agents panel**: the player pane shows up as an agent, titled with the current track
- **Sidebar line**: `▶ Song — Artist` under the workspace you're in, with no pane open
- **CLI**: `nowplaying next`, `nowplaying now`, … for scripts and key bindings

> **Not affiliated with Spotify.** This is an unofficial, community plugin. "Spotify" is a trademark
> of Spotify AB, used here only to describe compatibility.

## Requirements

- herdr 0.7.5 or newer, macOS
- Python 3.9+ (the system `python3` works; standard library only)
- One playback backend:

| Backend | What plays the music | Setup | Spotify account |
|---|---|---|---|
| **Spotify app** (default fallback) | the Spotify desktop app | none | Free or Premium |
| **spotify_player** | the headless [`spotify_player`](https://github.com/aome510/spotify-player) daemon, no Spotify app needed | build + your own Spotify client ID | **Premium** |

The plugin picks `spotify_player` if it is installed, otherwise the Spotify app. Override with
`backend` in the config file.

## Install

```sh
herdr plugin install reidsolon/herdr-nowplaying
```

Optional: put the `nowplaying` command on your PATH.

```sh
ln -s "$(herdr plugin list --json | python3 -c 'import json,sys; print([p for p in json.load(sys.stdin)["result"]["plugins"] if p["plugin_id"]=="reidsolon.nowplaying"][0]["plugin_root"])')/bin/nowplaying" ~/.local/bin/nowplaying
```

### Option A: Spotify desktop app

Nothing else to do. Keep the Spotify app running (closing its window is fine; quitting it stops
playback). macOS will ask once to let herdr control Spotify.

### Option B: headless `spotify_player` (Premium)

1. **Build `spotify_player` with daemon support.** On macOS the daemon needs media control turned
   off at build time, so the Homebrew bottle won't work:

   ```sh
   cargo install spotify_player --locked --no-default-features --features daemon,rodio-backend
   ```

2. **Create your own Spotify client ID.** Spotify's Developer Policy expects each app to use its
   own credentials, and the shared default ID that `spotify_player` falls back to is heavily
   rate-limited (controls will lag or fail with `429`).
   - Open the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and **Create app**
   - Redirect URI: `http://127.0.0.1:8989/login`
   - APIs: tick **Web API**
   - Copy the **Client ID**

3. **Configure `spotify_player`** in `~/.config/spotify-player/app.toml`:

   ```toml
   client_id = "<your client id>"
   client_port = 18080          # default 8080 often clashes with dev servers
   enable_notify = false
   enable_media_control = false # required for daemon mode on macOS

   [device]
   name = "herdr"               # the Spotify Connect device name you'll see in Spotify
   device_type = "computer"
   bitrate = 160
   ```

4. **Log in once** and start the daemon:

   ```sh
   spotify_player authenticate
   nowplaying daemon
   nowplaying here     # move playback to the "herdr" device
   ```

## Usage

### CLI

```
nowplaying            open the player (split pane; switches to it if already open)
nowplaying popup      open the player as a popup
nowplaying play       play/pause            nowplaying next | prev   skip
nowplaying now        print what's playing  nowplaying web           open track in Spotify
nowplaying here       move playback to the spotify_player device
nowplaying sidebar    start the sidebar line (nowplaying sidebar stop to hide)
nowplaying daemon     start the spotify_player daemon
```

Without the symlink, every command is also a plugin action:
`herdr plugin action invoke --plugin reidsolon.nowplaying <open|open-popup|play-pause|next|previous|play-here>`.

### Player keys

| Key | Action | Key | Action |
|---|---|---|---|
| `space` | play / pause | `s` | shuffle |
| `n` / `p` | next / previous | `r` | repeat |
| `←` / `→` | seek ∓10s | `w` | open track in Spotify |
| `+` / `-` | volume | `t` | play on the spotify_player device |
| `o` | start the player/app | `q` | close the pane (music keeps playing) |

### Sidebar line

Add a row that renders the plugin's `$nowplaying` token to `~/.config/herdr/config.toml`, then
`herdr server reload-config` and `nowplaying sidebar`:

```toml
[ui.sidebar.spaces]
rows = [["state_icon", "workspace"], ["branch", "git_status"], ["$nowplaying"]]
```

The line follows the focused workspace and refreshes every ~10s (immediately after a control).

### Key bindings

```toml
[[keys.command]]
key = "prefix+m"
type = "plugin_action"
command = "reidsolon.nowplaying.open"          # or .open-popup
description = "open now playing"

[[keys.command]]
key = "prefix+alt+n"
type = "plugin_action"
command = "reidsolon.nowplaying.next"
description = "next track"
```

## Configuration

Copy [`config.example.toml`](config.example.toml) to the plugin config directory
(`herdr plugin config-dir reidsolon.nowplaying`) as `config.toml`.

| Key | Default | |
|---|---|---|
| `backend` | `"auto"` | `auto`, `spotify_player` or `applescript` |
| `spotify_player_bin` | `""` | path if not on PATH or in `~/.cargo/bin` |
| `autostart_player` | `false` | start the spotify_player daemon when the herdr server starts |
| `autostart_sidebar` | `false` | start the sidebar line when the herdr server starts |
| `sidebar_width` | `30` | max characters for the sidebar line |

## Privacy

This plugin collects nothing and makes no network requests of its own.

- It reads playback state from the Spotify app (AppleScript) or the local `spotify_player` CLI.
- It writes a pid file and a refresh marker to herdr's plugin state directory.
- It reads `spotify_player`'s local log only to detect rate-limit errors.
- Spotify login and tokens are handled entirely by `spotify_player` and stored in
  `~/.cache/spotify-player/`. To disconnect: delete that folder and remove the app under
  [Spotify account → Manage apps](https://www.spotify.com/account/apps/).

## Compliance notes

- **No credentials are shipped.** Each user registers their own client ID, as the
  [Spotify Developer Policy](https://developer.spotify.com/policy) requires one set of credentials per app.
- **Premium** is required by Spotify for playback through third-party players (Option B).
- **`spotify_player` is not part of this plugin.** It is a separate MIT-licensed project built on
  [librespot](https://github.com/librespot-org/librespot), an unofficial Spotify client
  library. You install and run it yourself; review its terms and Spotify's
  [Developer Terms](https://developer.spotify.com/terms) before using it.
- **Attribution**: Spotify metadata is shown alongside the Spotify name, and `w` / `nowplaying web`
  links back to the track on Spotify, per the
  [Design & Branding Guidelines](https://developer.spotify.com/documentation/design). Logos can't be
  drawn in a terminal, so the name is used in text.
- The plugin name uses "for Spotify" to describe compatibility, as Spotify's guidelines allow, and does not
  use Spotify logos.

## Troubleshooting

- **Controls lag or do nothing, the pane says "rate-limited (429)"**: you're on the shared default
  client ID. Set your own `client_id` (Option B, steps 2–3) and run `spotify_player authenticate` again.
- **Paused at 0:00 after a while**: `spotify_player` lost its connection to Spotify and reconnected
  paused. Run `nowplaying play`.
- **"player not running"**: `nowplaying daemon` (Option B), or open the Spotify app (Option A).
- **Logs**: `~/.cache/spotify-player/*.log`, `herdr plugin log list`.

## License

[MIT](LICENSE)

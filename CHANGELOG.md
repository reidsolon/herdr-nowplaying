# Changelog

## 0.2.2 — 2026-10-08

### Fixed
- Plays and controls failing with `404` after the Mac slept or changed networks. `spotify_player`
  keeps running and accepting commands, but its Spotify Connect device connection dies and Spotify
  can no longer reach it. The plugin now reads the daemon log after each command (the CLI reports
  success either way), and on a `404` restarts the daemon, waits until Spotify lists the device
  again, and retries once. Works from the pane, the CLI and plugin actions/key bindings.
- Rate-limit detection now sees the custom client's own failure, not only the ncspot fallback's.

## 0.2.1 — 2026-10-07

### Fixed
- Playback could silently stop working after `spotify_player` had been running for a long time: the
  daemon stayed connected to Spotify (and listed as a device) but stopped listening on its control
  port, so every command went to a throwaway client instead. The plugin now checks the control port
  before each command, restarts a daemon that has gone deaf (and says so), starts a stopped daemon
  when you press a key, and never lets a command fall through to a throwaway client.

## 0.2.0 — 2026-10-07

### Added
- **Search**: press `/` in the player (or `nowplaying search [--play] <query>`) to find and play
  tracks, albums, artists and playlists.
- **Login prompt**: the player and CLI detect a missing `spotify_player` login and offer to log in
  with your browser (`a` in the player, or `nowplaying login`). The startup hook shows a herdr
  notification instead of starting a daemon that would hang waiting for a login.
- `open` and `open-popup` plugin actions for key bindings.

### Changed
- The pane never blocks on Spotify: status and commands run in the background, play/pause and volume
  update instantly, and picked results show as "starting…" until Spotify confirms them.
- A play that Spotify didn't apply (e.g. after `spotify_player` reconnects under a new device id) is
  retried once, then reported instead of silently doing nothing.
- A failed search says so instead of showing "No results".
- Pasted text can no longer trigger single-key shortcuts or cancel the search box; `q` (not `Esc`)
  closes the player.

### Removed
- The sidebar now-playing line (`$nowplaying` token, `nowplaying sidebar`, `autostart_sidebar`,
  `sidebar_width`).

## 0.1.0 — 2026-10-06

First release: player pane (split or popup), agents panel entry, `nowplaying` CLI, and two
backends: the `spotify_player` daemon or the Spotify desktop app (AppleScript).

# Changelog

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

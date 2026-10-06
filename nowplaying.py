#!/usr/bin/env python3
"""Now Playing for Spotify — a herdr plugin.

Shows what Spotify is playing in a herdr pane and the agents panel, and controls playback.

Backends:
  spotify_player  the `spotify_player -d` daemon (https://github.com/aome510/spotify-player), driven via its CLI
  applescript     the Spotify desktop app on macOS

Python 3.9+ standard library only.
"""
import curses
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "reidsolon.nowplaying")
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.expanduser(f"~/.config/herdr/plugins/config/{PLUGIN_ID}")
SEP = "\x1f"

DEFAULTS = {
    "backend": "auto",            # auto | spotify_player | applescript
    "spotify_player_bin": "",     # empty = find on PATH or ~/.cargo/bin
    "autostart_player": False,    # start the spotify_player daemon when the herdr server starts
}


def parse_toml(path):
    """Minimal TOML reader (tomllib needs Python 3.11): [sections] and scalar key = value lines."""
    try:
        import tomllib
        with open(path, "rb") as f:
            return tomllib.load(f)
    except ImportError:
        pass
    except (OSError, ValueError):
        return {}
    data_root = {}
    data = data_root
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if m := re.fullmatch(r"\[([\w.-]+)\]", line):
            data = data_root.setdefault(m.group(1), {})
        elif m := re.fullmatch(r"([\w-]+)\s*=\s*(.+)", line):
            raw = m.group(2).strip()
            if raw in ("true", "false"):
                val = raw == "true"
            elif re.fullmatch(r"-?\d+", raw):
                val = int(raw)
            else:
                val = raw.strip("\"'")
            data[m.group(1)] = val
    return data_root


CONFIG = {**DEFAULTS, **parse_toml(os.path.join(CONFIG_DIR, "config.toml"))}


def run(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return r.stdout.strip()


def open_url(url):
    if url:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", url], capture_output=True)


class SpotifyPlayerBackend:
    """Talks to a `spotify_player -d` daemon. Each status call is a Spotify Web API request,
    so callers poll sparingly and interpolate progress locally."""

    name = "spotify_player"
    poll_seconds = 5

    def __init__(self, binary):
        self.bin = binary
        sp_config = parse_toml(os.path.expanduser("~/.config/spotify-player/app.toml"))
        self.device_name = (sp_config.get("device") or {}).get("name", "spotify-player")

    def cli(self, *args):
        return run([self.bin, *args])

    def daemon_running(self):
        return subprocess.run(["pgrep", "-f", "spotify_player -d"], capture_output=True).returncode == 0

    def start(self):
        if not self.daemon_running():
            subprocess.run([self.bin, "-d"], capture_output=True)

    def status(self):
        if not self.daemon_running():
            return {"state": "NOT_RUNNING"}
        try:
            d = json.loads(self.cli("get", "key", "playback") or "null")
        except json.JSONDecodeError:
            d = None
        item = (d or {}).get("item")
        if not item:
            return {"state": "NO_TRACK"}
        device = d.get("device") or {}
        return {
            "state": "playing" if d.get("is_playing") else "paused",
            "name": item.get("name", ""),
            "artist": ", ".join(a["name"] for a in item.get("artists", [])),
            "album": (item.get("album") or {}).get("name", ""),
            "duration": item.get("duration_ms", 0) / 1000,
            "position": (d.get("progress_ms") or 0) / 1000,
            "volume": device.get("volume_percent") or 0,
            "shuffle": bool(d.get("shuffle_state")),
            "repeat": d.get("repeat_state", "off"),
            "device": device.get("name", ""),
            "url": (item.get("external_urls") or {}).get("spotify", ""),
        }

    def rate_limited_since(self, since):
        """The CLI exits 0 even when Spotify rejects a request, so check the daemon log for 429s."""
        logs = sorted(glob.glob(os.path.expanduser("~/.cache/spotify-player/*.log")), key=os.path.getmtime)
        if not logs or os.path.getmtime(logs[-1]) < since:
            return False
        with open(logs[-1], "rb") as f:
            f.seek(max(0, os.path.getsize(logs[-1]) - 4000))
            return any(b"429 Too Many Requests" in line for line in f.read().splitlines()[-2:])

    def search(self, query):
        """Returns {category: [{"kind", "id", "title", "sub"}]} for tracks, albums, artists and playlists,
        or None if the request failed (rate limit, timeout, daemon unreachable)."""
        try:
            d = json.loads(self.cli("search", query) or "null")
        except json.JSONDecodeError:
            d = None
        if not isinstance(d, dict):
            return None
        names = lambda artists: ", ".join(a.get("name", "") for a in artists or [])
        return {
            "Tracks": [{"kind": "track", "id": t["id"], "title": t["name"],
                        "sub": f"{names(t.get('artists'))} · {(t.get('album') or {}).get('name', '')}"}
                       for t in d.get("tracks", [])],
            "Albums": [{"kind": "album", "id": a["id"], "title": a["name"],
                        "sub": f"{names(a.get('artists'))} · {a.get('release_date', '')[:4]}"}
                       for a in d.get("albums", [])],
            "Artists": [{"kind": "artist", "id": a["id"], "title": a["name"], "sub": ""}
                        for a in d.get("artists", [])],
            "Playlists": [{"kind": "playlist", "id": p["id"], "title": p["name"], "sub": p.get("desc", "")}
                          for p in d.get("playlists", [])],
        }

    def play_item(self, item):
        if item["kind"] == "track":
            self.cli("playback", "start", "track", "--id", item["id"])
        else:
            self.cli("playback", "start", "context", "--id", item["id"], item["kind"])

    def command(self, action, amount=0):
        pb = ["playback"]
        argv = {
            "playpause": pb + ["play-pause"],
            "next": pb + ["next"],
            "previous": pb + ["previous"],
            "seek": pb + ["seek", "--", str(amount * 1000)],
            "volume": pb + ["volume", str(max(0, min(100, amount)))],
            "shuffle": pb + ["shuffle"],
            "repeat": pb + ["repeat"],
            "transfer": ["connect", "--name", self.device_name],
        }.get(action)
        if action == "start":
            self.start()
        elif argv:
            self.cli(*argv)


class AppleScriptBackend:
    """Talks to the Spotify desktop app (macOS)."""

    name = "applescript"
    poll_seconds = 3
    device_name = "Spotify app"

    STATUS_SCRIPT = f'''
    if application "Spotify" is not running then return "NOT_RUNNING"
    tell application "Spotify"
      try
        set t to current track
        set n to name of t
      on error
        return "NO_TRACK"
      end try
      return (player state as string) & "{SEP}" & (name of t) & "{SEP}" & (artist of t) & "{SEP}" & (album of t) & "{SEP}" & (duration of t) & "{SEP}" & (player position) & "{SEP}" & (sound volume) & "{SEP}" & (shuffling) & "{SEP}" & (repeating) & "{SEP}" & (spotify url of t)
    end tell
    '''

    search = None  # the desktop app has no search API; the UI opens the app's search instead

    def tell(self, cmd):
        return run(["osascript", "-e", f'if application "Spotify" is running then tell application "Spotify" to {cmd}'])

    def start(self):
        subprocess.run(["open", "-g", "-a", "Spotify"])

    def status(self):
        out = run(["osascript", "-e", self.STATUS_SCRIPT])
        if out in ("NOT_RUNNING", "NO_TRACK", ""):
            return {"state": out or "NOT_RUNNING"}
        parts = out.split(SEP)
        if len(parts) != 10:
            return {"state": "NO_TRACK"}
        state, name, artist, album, dur, pos, vol, shuf, rep, uri = parts
        return {
            "state": state,
            "name": name,
            "artist": artist,
            "album": album,
            "duration": int(dur) / 1000,
            "position": float(pos.replace(",", ".")),
            "volume": int(vol),
            "shuffle": shuf == "true",
            "repeat": "on" if rep == "true" else "off",
            "device": self.device_name,
            "url": uri,
        }

    def command(self, action, amount=0):
        script = {
            "playpause": "playpause",
            "next": "next track",
            "previous": "previous track",
            "seek": f"set player position to (player position + {amount})",
            "volume": f"set sound volume to {max(0, min(100, amount))}",
            "shuffle": "set shuffling to not shuffling",
            "repeat": "set repeating to not repeating",
        }.get(action)
        if action == "start":
            self.start()
        elif script:
            self.tell(script)


def pick_backend():
    choice = os.environ.get("NOWPLAYING_BACKEND") or CONFIG["backend"]
    binary = (CONFIG["spotify_player_bin"] or shutil.which("spotify_player")
              or os.path.expanduser("~/.cargo/bin/spotify_player"))
    if choice != "applescript" and os.access(binary, os.X_OK):
        return SpotifyPlayerBackend(binary)
    if choice == "spotify_player":
        sys.exit(f"spotify_player not found (looked for {binary}); set spotify_player_bin in {CONFIG_DIR}/config.toml")
    return AppleScriptBackend()


def herdr_bin():
    return os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or "herdr"


class AgentReporter:
    """Shows the player pane in herdr's agents panel, titled with the current track."""

    AGENT = "nowplaying"

    def __init__(self):
        self.pane = os.environ.get("HERDR_PANE_ID")
        self.herdr = herdr_bin()
        self.last = None

    def _run(self, subcommand, *args):
        if self.pane:
            subprocess.run([self.herdr, "pane", subcommand, self.pane, *args], capture_output=True)

    def update(self, s):
        if s["state"] in ("playing", "paused"):
            title = f"{state_icon(s)} {s['name']} — {s['artist']}"
        elif s["state"] == "NO_TRACK":
            title = "nothing playing"
        else:
            title = "player not running"
        if title == self.last:
            return
        self.last = title
        # Always "idle" so play/pause changes never fire herdr's agent-finished notifications.
        self._run("report-agent", "--source", PLUGIN_ID, "--agent", self.AGENT, "--state", "idle")
        self._run("report-metadata", "--source", PLUGIN_ID, "--agent", self.AGENT,
                  "--display-agent", "Now Playing · Spotify", "--title", title)

    def release(self):
        self._run("release-agent", "--source", PLUGIN_ID, "--agent", self.AGENT)


def state_icon(s):
    return "▶" if s["state"] == "playing" else "⏸"


def fmt(secs):
    secs = max(0, int(secs))
    return f"{secs // 60}:{secs % 60:02d}"


def put(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w:
        win.addnstr(y, x, text, max(0, w - x - 1), attr)


def draw(win, s, backend):
    win.erase()
    h, w = win.getmaxyx()
    accent = curses.color_pair(1) | curses.A_BOLD
    dim = curses.A_DIM

    # Attribution: Spotify metadata is always shown alongside the Spotify name.
    put(win, 0, 2, "♫ Now Playing · Spotify", accent)

    if s["state"] == "NOT_RUNNING":
        what = "The spotify_player daemon" if backend.name == "spotify_player" else "The Spotify app"
        put(win, 2, 2, f"{what} isn't running.  [o] start it   [q] quit")
        return
    if s["state"] == "NO_TRACK":
        msg = f"Nothing playing.  [/] search   [t] play on {backend.device_name}   [q] quit" if backend.name == "spotify_player" \
            else "Nothing queued.  [space] play   [q] quit"
        put(win, 2, 2, msg)
        return

    put(win, 1, 2, f"on {s['device']}", dim)
    put(win, 3, 2, f"{state_icon(s)}  {s['name']}", curses.A_BOLD)
    put(win, 4, 5, s["artist"])
    put(win, 5, 5, s["album"], dim)

    bar_w = max(10, w - 18)
    frac = min(1.0, s["position"] / s["duration"]) if s["duration"] else 0
    filled = int(bar_w * frac)
    put(win, 7, 2, fmt(s["position"]), dim)
    put(win, 7, 8, "━" * filled, accent)
    put(win, 7, 8 + filled, "─" * (bar_w - filled), dim)
    put(win, 7, 9 + bar_w, fmt(s["duration"]), dim)

    flags = f"vol {s['volume']:>3}%   shuffle {'on ' if s['shuffle'] else 'off'}   repeat {s['repeat']}"
    put(win, 9, 2, flags, dim)
    hints = ["space play/pause", "/ search", "n/p next/prev", "←/→ seek", "+/- vol", "s shuffle", "r repeat",
             "w open in Spotify"]
    if backend.name == "spotify_player" and s["device"] != backend.device_name:
        hints.append(f"t play on {backend.device_name}")
    hints.append("q quit")
    y, line = 11, ""
    for hint in hints:
        candidate = f"{line}  {hint}" if line else hint
        if line and len(candidate) > w - 4:
            put(win, y, 2, line, dim)
            y, line = y + 1, hint
        else:
            line = candidate
    put(win, y, 2, line, dim)


KEYS = {
    ord(" "): ("playpause", 0),
    ord("n"): ("next", 0), ord("l"): ("next", 0),
    ord("p"): ("previous", 0), ord("h"): ("previous", 0),
    curses.KEY_RIGHT: ("seek", 10), curses.KEY_LEFT: ("seek", -10),
    ord("+"): ("volume", 10), ord("="): ("volume", 10),
    ord("-"): ("volume", -10), ord("_"): ("volume", -10),
    ord("s"): ("shuffle", 0), ord("r"): ("repeat", 0),
    ord("t"): ("transfer", 0), ord("o"): ("start", 0),
}


def lone_escape(stdscr, drop_paste=False):
    """Call after reading ESC. Swallows the rest of an escape sequence and returns True only for a
    real Esc key press. Terminals (and herdr) wrap pasted text in ESC[200~ ... ESC[201~; with
    drop_paste the pasted text is discarded too, so it can't trigger single-key shortcuts."""
    stdscr.nodelay(True)
    try:
        first = stdscr.getch()
        if first == -1:
            return True
        seq = ""
        if first in (ord("["), ord("O")):
            while True:  # CSI/SS3: parameters until a final byte (letter or ~)
                ch = stdscr.getch()
                if ch == -1:
                    break
                seq += chr(ch)
                if chr(ch).isalpha() or ch == ord("~"):
                    break
        if drop_paste and seq == "200~":
            tail, deadline = "", time.monotonic() + 2
            while not tail.endswith("\x1b[201~") and time.monotonic() < deadline:
                ch = stdscr.getch()
                if ch != -1:
                    tail = (tail + chr(ch))[-6:]
        return False
    finally:
        stdscr.timeout(500)


def read_query(stdscr, prompt):
    """Single-line input on the bottom row. Returns the text, or None on Esc."""
    h, w = stdscr.getmaxyx()
    text = ""
    curses.curs_set(1)
    try:
        while True:
            stdscr.move(h - 1, 0)
            stdscr.clrtoeol()
            line = f"{prompt}{text}"[-(w - 3):]
            put(stdscr, h - 1, 2, line, curses.A_BOLD)
            stdscr.move(h - 1, min(w - 2, 2 + len(line)))
            stdscr.refresh()
            try:
                ch = stdscr.get_wch()
            except curses.error:
                continue
            if ch == "\x1b":
                if lone_escape(stdscr):
                    return None
                continue
            if ch in ("\n", "\r", curses.KEY_ENTER):
                return text.strip() or None
            if ch in ("\x7f", "\b", curses.KEY_BACKSPACE):
                text = text[:-1]
            elif ch == "\x15":  # ctrl+u clears
                text = ""
            elif isinstance(ch, str) and ch.isprintable():
                text += ch
    finally:
        curses.curs_set(0)


def search_screen(stdscr, backend):
    """Search Spotify and play a result. Returns True if something was started."""
    query = read_query(stdscr, "search: ")
    if not query:
        return False
    if not backend.search:
        open_url("spotify:search:" + urllib.parse.quote(query))
        return False

    accent = curses.color_pair(1) | curses.A_BOLD
    dim = curses.A_DIM
    stdscr.erase()
    put(stdscr, 0, 2, "♫ Search · Spotify", accent)
    put(stdscr, 2, 2, f"searching for “{query}”…", dim)
    stdscr.refresh()
    results = backend.search(query)
    failed = results is None
    results = results or {}
    tabs = [t for t in ("Tracks", "Albums", "Artists", "Playlists") if results.get(t)]
    tab, sel, top = 0, 0, 0

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        put(stdscr, 0, 2, f"♫ Search · Spotify  “{query}”", accent)
        if not tabs:
            msg = "Search failed (Spotify may be rate-limiting)." if failed else "No results."
            put(stdscr, 2, 2, f"{msg}  [/] search again   [esc] back")
        else:
            x = 2
            for i, name in enumerate(tabs):
                label = f" {name} ({len(results[name])}) "
                put(stdscr, 1, x, label, curses.A_REVERSE if i == tab else dim)
                x += len(label) + 1
            items = results[tabs[tab]]
            rows_per = 2
            visible = max(1, (h - 5) // rows_per)
            top = min(max(top, sel - visible + 1), sel)
            for row, item in enumerate(items[top:top + visible]):
                i = top + row
                y = 3 + row * rows_per
                marker = "›" if i == sel else " "
                put(stdscr, y, 2, f"{marker} {item['title']}", curses.A_BOLD if i == sel else 0)
                if item["sub"]:
                    put(stdscr, y + 1, 4, item["sub"], dim)
            put(stdscr, h - 1, 2, "↑/↓ select  tab category  enter play  / new search  esc back", dim)
        stdscr.refresh()

        k = stdscr.getch()
        if k == 27 and not lone_escape(stdscr, drop_paste=True):
            continue
        if k in (27, ord("q")):
            return False
        if k == ord("/"):
            return search_screen(stdscr, backend)
        if not tabs:
            continue
        items = results[tabs[tab]]
        if k in (curses.KEY_DOWN, ord("j")):
            sel = min(len(items) - 1, sel + 1)
        elif k in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
        elif k in (ord("\t"), curses.KEY_RIGHT, ord("l")):
            tab, sel, top = (tab + 1) % len(tabs), 0, 0
        elif k in (curses.KEY_BTAB, curses.KEY_LEFT, ord("h")):
            tab, sel, top = (tab - 1) % len(tabs), 0, 0
        elif k in (10, 13, curses.KEY_ENTER):
            backend.play_item(items[sel])
            return True


def ui(stdscr):
    curses.set_escdelay(25)
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    stdscr.timeout(500)

    reporter = AgentReporter()
    try:
        loop(stdscr, pick_backend(), reporter)
    finally:
        reporter.release()


def loop(stdscr, backend, reporter):
    notice, notice_until = "", 0
    fetched = backend.status()
    fetched_at = time.monotonic()
    while True:
        now = time.monotonic()
        # Interpolate progress between polls; refetch on schedule or when the track should have ended.
        s = dict(fetched)
        if s["state"] == "playing":
            s["position"] = min(s["duration"], s["position"] + now - fetched_at)
        track_over = s["state"] == "playing" and s["position"] >= s["duration"]
        if now - fetched_at >= backend.poll_seconds or track_over:
            fetched, fetched_at = backend.status(), time.monotonic()
            continue

        reporter.update(s)
        draw(stdscr, s, backend)
        if notice and now < notice_until:
            put(stdscr, stdscr.getmaxyx()[0] - 1, 2, notice, curses.color_pair(2) | curses.A_BOLD)
        stdscr.refresh()

        k = stdscr.getch()
        if k == 27:
            lone_escape(stdscr, drop_paste=True)  # ignore escape sequences and pasted text; q quits
            continue
        if k == ord("q"):
            return
        if k == ord("w"):
            open_url(s.get("url"))
        elif k == ord("/"):
            if search_screen(stdscr, backend):
                time.sleep(0.5)
                fetched, fetched_at = backend.status(), time.monotonic()
                fetched_at -= max(0, backend.poll_seconds - 1)
        elif k in KEYS:
            action, amount = KEYS[k]
            if action == "volume":
                amount += s.get("volume", 0)  # absolute target, clamped by the backend
            sent = time.time()
            backend.command(action, amount)
            time.sleep(0.3)
            if getattr(backend, "rate_limited_since", lambda _: False)(sent):
                notice, notice_until = "Spotify rate-limited that request (429). Try again shortly.", now + 4
            fetched, fetched_at = backend.status(), time.monotonic()
            # Spotify often still reports the old track right after a change; check again in ~1s.
            fetched_at -= max(0, backend.poll_seconds - 1)


# --- CLI ---------------------------------------------------------------------------------------

USAGE = """usage: nowplaying [command]

  (none)          open the player (herdr split pane; plain terminal outside herdr)
  popup           open the player as a herdr popup
  play | pause    toggle play/pause
  next | prev     skip track
  here            move playback to the spotify_player device
  now             print what's playing
  web             open the current track in Spotify
  search [--play] <query>
                  search Spotify (spotify_player); --play starts the top track
  daemon          start the spotify_player daemon if it isn't running"""


def open_pane(entrypoint):
    if not os.environ.get("HERDR_ENV"):
        curses.wrapper(ui)
        return
    herdr = herdr_bin()
    try:
        panes = json.loads(run([herdr, "pane", "list"]))["result"]["panes"]
    except (json.JSONDecodeError, KeyError, TypeError):
        panes = []
    here = os.environ.get("HERDR_WORKSPACE_ID")
    for p in panes:
        if p.get("agent") != AgentReporter.AGENT:
            continue
        if p.get("workspace_id") == here:
            subprocess.run([herdr, "plugin", "pane", "focus", p["pane_id"]], capture_output=True)
            return
        # The player lives in another workspace: bring it here instead of jumping there.
        subprocess.run([herdr, "pane", "close", p["pane_id"]], capture_output=True)
    argv = [herdr, "plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", entrypoint]
    caller = os.environ.get("HERDR_PANE_ID")
    if entrypoint == "player-split" and caller:
        argv += ["--target-pane", caller]  # split next to the pane we were run from; popups use the active pane
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr.strip() or r.stdout.strip())


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "open"
    cmd = {"play": "playpause", "pause": "playpause", "prev": "previous", "here": "transfer"}.get(cmd, cmd)
    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return
    if cmd == "open":
        open_pane("player-split")
    elif cmd == "popup":
        open_pane("player")
    elif cmd == "ui":
        curses.wrapper(ui)
    elif cmd == "daemon":
        pick_backend().start()
    elif cmd == "startup":  # herdr [[startup]] hook: one-shot, opt-in via config
        if CONFIG["autostart_player"]:
            pick_backend().start()
    elif cmd in ("now", "web"):
        s = pick_backend().status()
        if s["state"] not in ("playing", "paused"):
            print("nothing playing" if s["state"] == "NO_TRACK" else "player not running")
        elif cmd == "web":
            open_url(s["url"])
        else:
            print(f"{state_icon(s)} {s['name']} — {s['artist']}  [{fmt(s['position'])}/{fmt(s['duration'])}] on {s['device']}")
    elif cmd == "search":
        args = sys.argv[2:]
        play = "--play" in args
        query = " ".join(a for a in args if a != "--play").strip()
        if not query:
            sys.exit("usage: nowplaying search [--play] <query>")
        backend = pick_backend()
        if not backend.search:
            open_url("spotify:search:" + urllib.parse.quote(query))
            return
        results = backend.search(query)
        if results is None:
            sys.exit("search failed (Spotify may be rate-limiting); try again")
        tracks = results.get("Tracks", [])
        if play:
            if not tracks:
                sys.exit("no tracks found")
            backend.play_item(tracks[0])
            print(f"▶ {tracks[0]['title']} — {tracks[0]['sub']}")
            return
        for name in ("Tracks", "Albums", "Artists", "Playlists"):
            items = results.get(name, [])[:5]
            if items:
                print(name)
                for item in items:
                    print(f"  {item['title']}" + (f"  —  {item['sub']}" if item["sub"] else ""))
    elif cmd in ("playpause", "next", "previous", "transfer"):
        pick_backend().command(cmd)
    else:
        sys.exit(f"unknown command: {cmd}\n\n{USAGE}")


if __name__ == "__main__":
    main()

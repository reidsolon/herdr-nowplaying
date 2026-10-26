#!/usr/bin/env python3
"""Now Playing for Spotify — a herdr plugin.

Shows what Spotify is playing in a herdr pane and the agents panel, and controls playback.

Backends:
  spotify_player  the `spotify_player -d` daemon (https://github.com/aome510/spotify-player), driven via its CLI
  applescript     the Spotify desktop app on macOS

Python 3.9+ standard library only.
"""
import base64
import curses
import datetime
import glob
import hashlib
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "reidsolon.nowplaying")
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.expanduser(f"~/.config/herdr/plugins/config/{PLUGIN_ID}")
SEP = "\x1f"

DEFAULTS = {
    "backend": "auto",            # auto | spotify_player | applescript
    "spotify_player_bin": "",     # empty = find on PATH or ~/.cargo/bin
    "autostart_player": False,    # start the spotify_player daemon when the herdr server starts
    "bar_controls": True,         # `nowplaying bar`: show key hints before the track
    "bar_key_previous": ",",      # hint text for each control; "" hides that hint
    "bar_key_play_pause": "Space",
    "bar_key_next": ".",
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
        # Discard output rather than capture it: capturing waits for anything the opener left running.
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", url], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


NCSPOT_CLIENT_ID = "d420a117a32841c2b3474932e49fb54b"  # spotify_player's default Web API client
# ncspot's app only accepts this redirect since ncspot 2026-09-28 ("Separate ncspot from Spotify login"),
# but spotify_player 0.25.1 still asks it for http://127.0.0.1:8989/login, so its `authenticate` fails
# with "redirect_uri: Not matching configuration". The plugin does that one login itself.
NCSPOT_REDIRECT_URI = "http://127.0.0.1:8989/ncspot_login"
NCSPOT_SCOPES = ("playlist-read-collaborative user-read-recently-played user-read-playback-position "
                 "user-read-currently-playing app-remote-control playlist-modify-public playlist-modify-private "
                 "user-follow-read user-library-read streaming user-top-read user-follow-modify "
                 "user-read-playback-state playlist-read-private user-modify-playback-state user-library-modify")
SPOTIFY_ACCOUNTS = os.environ.get("NOWPLAYING_SPOTIFY_ACCOUNTS", "https://accounts.spotify.com")  # overridable for tests


class SpotifyPlayerBackend:
    """Talks to a `spotify_player -d` daemon. Each status call is a Spotify Web API request,
    so callers poll sparingly and interpolate progress locally."""

    name = "spotify_player"
    poll_seconds = 5

    def __init__(self, binary):
        self.bin = binary
        sp_config = parse_toml(os.path.expanduser("~/.config/spotify-player/app.toml"))
        self.device_name = (sp_config.get("device") or {}).get("name", "spotify-player")
        self.client_id = sp_config.get("client_id") or NCSPOT_CLIENT_ID
        self.cache_dir = os.path.expanduser("~/.cache/spotify-player")
        self.client_port = int(sp_config.get("client_port") or 8080)
        self.login_proc = None
        self.login_cancelled = False
        self.heal_lock = threading.Lock()
        # None = never. Not 0.0: on Python 3.9 macOS, time.monotonic() starts near 0 with the process.
        self.last_restart = None
        self.last_reconnect = None
        self.healed = False  # set when a stuck daemon was restarted; the UI reports it once

    def cli(self, *args):
        # If the daemon isn't reachable, the spotify_player CLI quietly starts a throwaway client to
        # answer instead, so commands "succeed" without touching the real player. Never let that happen.
        if not self.ensure_ready():
            return ""
        return run([self.bin, *args])

    def daemon_running(self):
        return subprocess.run(["pgrep", "-f", "spotify_player -d"], capture_output=True).returncode == 0

    def control_reachable(self):
        """True if something is listening on the daemon's control port (UDP client_port). Binding it
        ourselves succeeds only when nothing is."""
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind(("127.0.0.1", self.client_port))
            return False
        except OSError:
            return True
        finally:
            probe.close()

    def ensure_ready(self, start_if_stopped=True):
        """Make sure the daemon is running and listening. Restarts a daemon that is still connected to
        Spotify but no longer accepts commands (seen after long uptimes with many reconnects)."""
        if self.control_reachable():
            return True
        if self.missing_logins():
            return False
        with self.heal_lock:
            if self.control_reachable():
                return True
            running = self.daemon_running()
            if not running and not start_if_stopped:
                return False
            if self.last_restart is not None and time.monotonic() - self.last_restart < 30:
                return False  # just tried; don't loop restarting
            self.last_restart = time.monotonic()
            if running:
                subprocess.run(["pkill", "-f", "spotify_player -d"], capture_output=True)
                for _ in range(20):
                    if not self.daemon_running():
                        break
                    time.sleep(0.1)
                self.healed = True
            subprocess.run([self.bin, "-d"], capture_output=True)
            for _ in range(40):
                if self.control_reachable():
                    return True
                time.sleep(0.125)
            return False

    def token_path(self, client_id):
        return os.path.join(self.cache_dir, f"{client_id}_token.json")

    def missing_logins(self):
        """spotify_player keeps up to three logins: librespot credentials for streaming, a Web API
        token for the configured client id, and (with your own client id) a token for ncspot's app,
        its backup for a few endpoints. Any one missing means it can't work until the user logs in."""
        missing = []
        if not os.path.exists(os.path.join(self.cache_dir, "credentials.json")):
            missing.append("streaming")
        if not os.path.exists(self.token_path(self.client_id)):
            missing.append("Web API")
        if self.client_id != NCSPOT_CLIENT_ID and not os.path.exists(self.token_path(NCSPOT_CLIENT_ID)):
            missing.append("backup Web API")
        return missing

    def login(self, on_url=lambda url: None):
        """Log in to whatever is missing; one browser tab per login. Returns True once everything is
        logged in and the daemon is up.

        Not `spotify_player authenticate`: it always redoes ncspot's login with a redirect URI that
        ncspot's app rejects. Instead this does ncspot's login itself, then runs spotify_player
        normally, which only asks for the logins it doesn't have yet."""
        self.login_cancelled = False
        # A daemon started without a login sits on the callback port waiting for one; stop it first.
        subprocess.run(["pkill", "-f", "spotify_player -d"], capture_output=True)
        if "backup Web API" in self.missing_logins() and not self.ncspot_login(on_url):
            return False
        if self.missing_logins():
            self.run_until_logged_in(on_url)
        ok = not self.missing_logins() and not self.login_cancelled
        if ok:
            self.start()
        return ok

    def run_until_logged_in(self, on_url):
        """Run spotify_player in the foreground: on start it logs in to anything missing (printing
        "Browse to: <url>"), then would carry on into its UI, so stop it once the logins exist."""
        self.login_proc = proc = subprocess.Popen([self.bin], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                  stderr=subprocess.STDOUT, text=True, errors="replace")

        def read():
            for line in proc.stdout:
                m = re.search(r"Browse to: (\S+)", line)
                if m:
                    on_url(m.group(1))
        threading.Thread(target=read, daemon=True).start()
        try:
            while proc.poll() is None and self.missing_logins() and not self.login_cancelled:
                time.sleep(0.2)
            if proc.poll() is None:
                time.sleep(0.5)  # let it finish writing the last login
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self.login_proc = None

    def ncspot_login(self, on_url):
        """Authorization code + PKCE login for ncspot's app on the redirect URI it accepts, saving the
        token where spotify_player reads it (it refreshes the token itself from then on)."""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        url = f"{SPOTIFY_ACCOUNTS}/authorize?" + urllib.parse.urlencode({
            "client_id": NCSPOT_CLIENT_ID, "response_type": "code", "redirect_uri": NCSPOT_REDIRECT_URI,
            "code_challenge_method": "S256", "code_challenge": challenge, "state": state, "scope": NCSPOT_SCOPES})
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            parsed = urllib.parse.urlparse(NCSPOT_REDIRECT_URI)
            listener.bind((parsed.hostname, parsed.port))
            listener.listen(8)
            listener.settimeout(0.3)
        except OSError:
            listener.close()
            return False
        open_url(url)
        on_url(url)
        code, deadline = None, time.monotonic() + 600
        try:
            while code is None and not self.login_cancelled and time.monotonic() < deadline:
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                with conn:
                    conn.settimeout(2)
                    try:
                        target = conn.recv(8192).decode("latin-1").split(" ")[1]
                    except (OSError, IndexError):
                        continue
                    query = urllib.parse.parse_qs(urllib.parse.urlparse(target).query)
                    if urllib.parse.urlparse(target).path != parsed.path or query.get("state") != [state]:
                        conn.sendall(b"HTTP/1.1 404 Not Found\r\ncontent-length: 0\r\nconnection: close\r\n\r\n")
                        continue  # browser prefetches (favicon) and stray requests
                    ok = "code" in query
                    body = ("Logged in. You can close this tab and go back to herdr." if ok
                            else f"Spotify login failed: {query.get('error', ['unknown'])[0]}. Go back to herdr and try again.")
                    conn.sendall(f"HTTP/1.1 200 OK\r\ncontent-type: text/plain; charset=utf-8\r\n"
                                 f"content-length: {len(body.encode())}\r\nconnection: close\r\n\r\n{body}".encode())
                    if not ok:
                        return False
                    code = query["code"][0]
        finally:
            listener.close()
        if code is None:
            return False
        try:
            token = self.exchange_code(code, verifier, NCSPOT_REDIRECT_URI)
        except (OSError, ValueError):
            return False
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=token["expires_in"])
        path = self.token_path(NCSPOT_CLIENT_ID)
        os.makedirs(self.cache_dir, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump({"access_token": token["access_token"], "expires_in": token["expires_in"],
                       "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                       "refresh_token": token.get("refresh_token"), "scope": token.get("scope", NCSPOT_SCOPES)}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True

    @staticmethod
    def exchange_code(code, verifier, redirect_uri):
        data = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                                       "client_id": NCSPOT_CLIENT_ID, "code_verifier": verifier}).encode()
        req = urllib.request.Request(f"{SPOTIFY_ACCOUNTS}/api/token", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    def cancel_login(self):
        self.login_cancelled = True
        if self.login_proc:
            self.login_proc.terminate()

    def start(self):
        self.last_restart = None
        self.ensure_ready()

    def status(self, heal=True):
        missing = self.missing_logins()
        if missing:
            return {"state": "NOT_AUTHENTICATED", "missing": missing}
        if not heal:
            # Read-only (the tab bar polls from a fresh process each time, so it has no restart backoff).
            if not self.control_reachable():
                return {"state": "NOT_RUNNING"}
            out = run([self.bin, "get", "key", "playback"])
        # Polling never starts a stopped daemon, but it does heal a running one that went deaf.
        elif not self.ensure_ready(start_if_stopped=False):
            return {"state": "NOT_RUNNING"}
        else:
            out = self.cli("get", "key", "playback")
        try:
            d = json.loads(out or "null")
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
            "id": item.get("id", ""),
            "context": (d.get("context") or {}).get("uri", ""),
        }

    def request_errors_since(self, since):
        """HTTP statuses of requests the daemon reported failing after `since` (epoch seconds). The CLI
        exits 0 even when Spotify rejects a request, so the daemon's log is the only signal. Includes the
        custom client's own failure (e.g. 404), not just the ncspot fallback's (often 429)."""
        logs = sorted(glob.glob(os.path.join(self.cache_dir, "*.log")), key=os.path.getmtime)
        if not logs or os.path.getmtime(logs[-1]) < since:
            return set()
        with open(logs[-1], "rb") as f:
            f.seek(max(0, os.path.getsize(logs[-1]) - 16000))
            lines = f.read().decode("utf-8", "replace").splitlines()
        errors = set()
        for line in lines:
            if "Failed to handle a player request" not in line and "Web API request failed" not in line:
                continue
            try:
                when = datetime.datetime.strptime(line[:26], "%Y-%m-%dT%H:%M:%S.%f").replace(
                    tzinfo=datetime.timezone.utc).timestamp()
            except ValueError:
                continue
            m = re.search(r"status(?: code)?[= ](\d{3})", line)
            if when >= since and m:
                errors.add(int(m.group(1)))
        return errors

    def rate_limited_since(self, since):
        return 429 in self.request_errors_since(since)

    def restart(self):
        """Restart the daemon so it registers a fresh Spotify Connect device. Needed when Spotify has lost
        the device (its dealer connection died after sleep/network changes) while the daemon still runs
        and accepts commands: every request then 404s. Returns True once Spotify lists the device again."""
        if self.last_reconnect is not None and time.monotonic() - self.last_reconnect < 60:
            return False  # one reconnect per minute at most
        self.last_reconnect = time.monotonic()
        with self.heal_lock:
            subprocess.run(["pkill", "-f", "spotify_player -d"], capture_output=True)
            for _ in range(20):
                if not self.daemon_running():
                    break
                time.sleep(0.1)
            self.last_restart = None
        if not self.ensure_ready():
            return False
        for _ in range(16):
            try:
                devices = json.loads(run([self.bin, "get", "key", "devices"]) or "[]")
            except json.JSONDecodeError:
                devices = []
            if any(d.get("name") == self.device_name for d in devices or []):
                return True
            time.sleep(0.5)
        return False

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
    login = None   # the desktop app handles its own login

    def missing_logins(self):
        return []

    def tell(self, cmd):
        return run(["osascript", "-e", f'if application "Spotify" is running then tell application "Spotify" to {cmd}'])

    def start(self):
        subprocess.run(["open", "-g", "-a", "Spotify"])

    def status(self, heal=True):
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
            "id": uri.rsplit(":", 1)[-1],
            "context": "",
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


def spotify_player_bin():
    return (CONFIG["spotify_player_bin"] or shutil.which("spotify_player")
            or os.path.expanduser("~/.cargo/bin/spotify_player"))


def pick_backend():
    choice = os.environ.get("NOWPLAYING_BACKEND") or CONFIG["backend"]
    binary = spotify_player_bin()
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
        if s["state"] == "LOADING":
            return
        if s["state"] in ("NOT_AUTHENTICATED", "AUTHENTICATING"):
            title = "logging in…" if s["state"] == "AUTHENTICATING" else "not logged in"
        elif s.get("starting"):
            title = f"… {s['name']}"
        elif s["state"] in ("playing", "paused"):
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


def clip(text, limit):
    text = " ".join("".join(c if c.isprintable() else " " for c in str(text)).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def bar_line(s):
    """One line for herdr's tab bar (a `ui.tab_bar_right` command entry). Empty output hides the entry."""
    if s["state"] == "NOT_AUTHENTICATED":
        return "Spotify · not logged in: nowplaying login"
    if s["state"] not in ("playing", "paused"):
        return ""
    track = clip(s["name"], 28)
    if s.get("artist"):
        track += " — " + clip(s["artist"], 20)
    track += f" · {fmt(s['position'])} / {fmt(s['duration'])}"
    if not CONFIG["bar_controls"]:
        return track
    hints = [("⏮", CONFIG["bar_key_previous"]),
             ("⏸" if s["state"] == "playing" else "▶", CONFIG["bar_key_play_pause"]),
             ("⏭", CONFIG["bar_key_next"])]
    controls = "   ".join(f"{icon} {key}" for icon, key in hints if key)
    return f"{controls}  │  {track}" if controls else track


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

    if s["state"] == "LOADING":
        put(win, 2, 2, "connecting…", dim)
        return
    if s["state"] == "NOT_AUTHENTICATED":
        put(win, 2, 2, "Not logged in to Spotify.", curses.A_BOLD)
        put(win, 3, 2, f"spotify_player needs your {' and '.join(s['missing'])} login.", dim)
        put(win, 5, 2, "[a] log in with your browser   [q] quit")
        return
    if s["state"] == "AUTHENTICATING":
        put(win, 2, 2, "Approve the login in your browser…", curses.A_BOLD)
        put(win, 3, 2, "Already-approved steps finish on their own; one tab may open per step.", dim)
        put(win, 5, 2, "[w] open the login page again   [esc] cancel")
        return
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
    put(win, 3, 2, f"{'…' if s.get('starting') else state_icon(s)}  {s['name']}", curses.A_BOLD)
    put(win, 4, 5, s["artist"])
    put(win, 5, 5, s["album"], dim)

    if s.get("starting"):
        put(win, 7, 2, "starting…", accent)
    else:
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
    return escape_sequence(stdscr, drop_paste) is None


def escape_sequence(stdscr, drop_paste=False):
    """Like lone_escape, but returns what followed ESC[ or ESC O (for example "B" for the down
    arrow, which curses doesn't always translate), or None for a real Esc key press."""
    stdscr.nodelay(True)
    try:
        first = stdscr.getch()
        if first == -1:
            return None
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
        return seq
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
    """Search Spotify. Returns the result the user picked, or None."""
    query = read_query(stdscr, "search: ")
    if not query:
        return None
    if not backend.search:
        open_url("spotify:search:" + urllib.parse.quote(query))
        return None

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
            return None
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
            return items[sel]


class Session:
    """Polls status and runs commands on background threads, so the UI never waits on Spotify.

    Playing a search result is optimistic: the pane shows it as "starting" right away, then
    confirms Spotify actually switched to it and retries once if not (e.g. spotify_player
    reconnected under a new device id and its first request 404'd)."""

    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.Lock()
        self.status, self.status_at = {"state": "LOADING"}, time.monotonic()
        self.pending = None  # {"item": ...} while a play is being confirmed
        self.authenticating = None  # {"url": ...} while a browser login is in progress
        self.notice, self.notice_until = "", 0
        self.wake = threading.Event()
        self.jobs = queue.Queue()
        threading.Thread(target=self._poll, daemon=True).start()
        threading.Thread(target=self._work, daemon=True).start()

    # background threads

    def _store(self, status):
        with self.lock:
            self.status, self.status_at = status, time.monotonic()

    def _poll(self):
        while True:
            self._store(self.backend.status())
            if getattr(self.backend, "healed", False):
                self.backend.healed = False
                self.say("spotify_player had stopped responding; restarted it.", 6)
            self.wake.wait(self.backend.poll_seconds)
            self.wake.clear()

    def _work(self):
        while True:
            job = self.jobs.get()
            try:
                job()
            except Exception as e:  # keep the worker alive; surface the problem in the pane
                self.say(f"Error: {e}")

    def refresh(self, delay=0.0):
        if delay:
            threading.Timer(delay, self.wake.set).start()
        else:
            self.wake.set()

    def say(self, text, seconds=5):
        with self.lock:
            self.notice, self.notice_until = text, time.monotonic() + seconds

    # actions

    def command(self, action, amount=0):
        with self.lock:
            s, now = self.status, time.monotonic()
            if action == "playpause" and s.get("state") in ("playing", "paused"):
                position = s["position"] + (now - self.status_at if s["state"] == "playing" else 0)
                self.status = dict(s, state="paused" if s["state"] == "playing" else "playing", position=position)
                self.status_at = now
            elif action == "volume" and "volume" in s:
                self.status = dict(s, volume=max(0, min(100, amount)))

        def job():
            sent = time.time()
            self.backend.command(action, amount)
            self.refresh(0.3)
            # Check the outcome off the queue so rapid key presses don't wait on it.
            threading.Thread(target=self._after_command, args=(action, amount, sent), daemon=True).start()
            self.refresh(1.5)  # Spotify often still reports the old track right after a change
        self.jobs.put(job)

    def play(self, item):
        # Own thread, so confirming never delays other keys; a newer play supersedes an older one.
        with self.lock:
            prev_id = self.status.get("id")
            self.pending = pending = {"item": item}
        threading.Thread(target=self._play_confirmed, args=(item, prev_id, pending), daemon=True).start()

    @staticmethod
    def _confirms(item, s, prev_id):
        if item["kind"] == "track":
            return s.get("id") == item["id"]
        return item["id"] in s.get("context", "") or (s.get("state") == "playing" and s.get("id") not in ("", prev_id))

    def _after_command(self, action, amount, sent):
        time.sleep(0.6)  # the daemon logs a failed request a few hundred ms after the CLI returns
        errors = self._errors_since(sent)
        if 404 in errors and self._reconnect():
            self.jobs.put(lambda: (self.backend.command(action, amount), self.refresh(0.3)))
        elif 429 in errors:
            self.say("Spotify rate-limited that request (429). Try again shortly.")

    def _errors_since(self, since):
        return getattr(self.backend, "request_errors_since", lambda _: set())(since)

    def _reconnect(self):
        """Spotify answered 404: it lost our Connect device. Restart the daemon to register it again."""
        restart = getattr(self.backend, "restart", None)
        if not restart:
            return False
        self.say("Spotify lost the player device; reconnecting…", 10)
        if restart():
            self.say("Reconnected the player device to Spotify.", 5)
            return True
        self.say("Couldn't reconnect the player device. Try again in a minute.", 8)
        return False

    def _play_confirmed(self, item, prev_id, pending):
        current = lambda: self.pending is pending
        for attempt in range(2):
            if not current():
                return
            sent = time.time()
            self.backend.play_item(item)
            time.sleep(1.0)
            if 404 in self._errors_since(sent) and attempt == 0 and self._reconnect():
                continue  # retry the play on the freshly registered device
            for check in range(2):
                if check:
                    time.sleep(1.0)
                s = self.backend.status()
                with self.lock:
                    if self.pending is not pending:
                        return
                    self.status, self.status_at = s, time.monotonic()
                    if self._confirms(item, s, prev_id):
                        self.pending = None
                        return
        with self.lock:
            if self.pending is not pending:
                return
            self.pending = None
        self.say(f"Couldn't start “{item['title']}”: Spotify didn't switch. Try again.", 8)

    def login(self):
        if self.authenticating or not self.backend.login:
            return
        self.authenticating = {"url": ""}

        def set_url(url):
            with self.lock:
                if self.authenticating:
                    self.authenticating = {"url": url}

        def job():
            ok = False
            try:
                ok = self.backend.login(on_url=set_url)
            finally:
                with self.lock:
                    cancelled = self.authenticating is None
                    self.authenticating = None
            if ok:
                self.say("Logged in to Spotify.")
            elif not cancelled:
                self.say("Login didn't finish. Press a to try again.", 8)
            self.refresh()
        threading.Thread(target=job, daemon=True).start()

    def cancel_login(self):
        with self.lock:
            self.authenticating = None
        self.backend.cancel_login()

    # what the UI shows

    def view(self):
        now = time.monotonic()
        with self.lock:
            s, at, pending, auth = dict(self.status), self.status_at, self.pending, self.authenticating
            notice = self.notice if now < self.notice_until else ""
        if auth:
            return {"state": "AUTHENTICATING", "url": auth["url"]}, notice
        if pending:
            item = pending["item"]
            if item["kind"] == "track":
                artist, _, album = item["sub"].partition(" · ")
            else:
                artist, album = item["kind"], item["sub"]
            s = {"state": "playing", "starting": True, "name": item["title"], "artist": artist, "album": album,
                 "duration": 0, "position": 0, "volume": s.get("volume", 0), "shuffle": s.get("shuffle", False),
                 "repeat": s.get("repeat", "off"), "device": s.get("device", ""), "url": "", "id": item["id"]}
        elif s.get("state") == "playing":
            s["position"] = min(s["duration"], s["position"] + now - at)
        return s, notice


def ui(stdscr, setup=False):
    curses.set_escdelay(25)
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    if setup or needs_setup():
        stdscr.timeout(500)
        if not Setup(stdscr).run():
            return
    stdscr.timeout(250)

    reporter = AgentReporter()
    try:
        loop(stdscr, pick_backend(), reporter)
    finally:
        reporter.release()


def loop(stdscr, backend, reporter):
    session = Session(backend)
    ended_id = None
    while True:
        s, notice = session.view()
        # Refetch once when the current track should have ended.
        if s.get("state") == "playing" and not s.get("starting") and s["position"] >= s["duration"] \
                and s.get("id") != ended_id:
            ended_id = s.get("id")
            session.refresh(0.5)

        reporter.update(s)
        draw(stdscr, s, backend)
        if notice:
            put(stdscr, stdscr.getmaxyx()[0] - 1, 2, notice, curses.color_pair(2) | curses.A_BOLD)
        stdscr.refresh()

        k = stdscr.getch()
        if k == 27:
            # Esc cancels a login in progress; other escape sequences and pasted text are ignored.
            if lone_escape(stdscr, drop_paste=True) and s["state"] == "AUTHENTICATING":
                session.cancel_login()
            continue
        if k == ord("q"):
            if s["state"] == "AUTHENTICATING":
                session.cancel_login()
            return
        if s["state"] in ("NOT_AUTHENTICATED", "AUTHENTICATING"):
            if k == ord("a"):
                session.login()
            elif k == ord("w"):
                open_url(s.get("url"))
            continue
        if k == ord("w"):
            open_url(s.get("url"))
        elif k == ord("/"):
            item = search_screen(stdscr, backend)
            if item:
                session.play(item)
        elif k in KEYS:
            action, amount = KEYS[k]
            if action == "volume":
                amount += s.get("volume", 0)  # absolute target, clamped by the backend
            session.command(action, amount)


# --- setup -------------------------------------------------------------------------------------
# Everything a new user would otherwise copy and paste from the README: building spotify_player,
# registering a Spotify client ID, logging in, the `nowplaying` command and herdr key bindings.

DASHBOARD_URL = "https://developer.spotify.com/dashboard"
REDIRECT_URI = "http://127.0.0.1:8989/login"  # spotify_player's default login_redirect_uri
RUSTUP_URL = "https://rustup.rs"
CARGO_INSTALL = ["install", "spotify_player", "--locked", "--no-default-features", "--features", "daemon,rodio-backend"]
LAUNCHER_MARK = "nowplaying launcher"
BAR_COMMAND = "~/.local/bin/nowplaying bar"

KEYBINDINGS = {  # (key, action, description)
    "player": [("prefix+m", "open-popup", "now playing")],
    "controls": [("prefix+comma", "previous", "previous track"),
                 ("prefix+space", "play-pause", "play / pause"),
                 ("prefix+period", "next", "next track")],
}
BAR_KEYS = {"previous": "bar_key_previous", "play-pause": "bar_key_play_pause", "next": "bar_key_next"}


def home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def plugin_config_path():
    return os.path.join(CONFIG_DIR, "config.toml")


def spotify_player_config_path():
    return home(".config", "spotify-player", "app.toml")


def herdr_config_path():
    return home(".config", "herdr", "config.toml")


def needs_setup():
    """Setup writes the plugin's config.toml, so a missing one means a fresh install."""
    return not os.path.exists(plugin_config_path())


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def write_text(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def toml_literal(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value)


def set_toml_keys(text, values, overwrite=True):
    """Set top-level keys in TOML text, leaving comments, other keys and sections as they are."""
    lines = text.splitlines()
    end = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    for key, value in values.items():
        line = f"{key} = {toml_literal(value)}"
        found = next((i for i in range(end) if re.match(rf"\s*{re.escape(key)}\s*=", lines[i])), None)
        if found is None:
            at = end
            while at > 0 and not lines[at - 1].strip():
                at -= 1
            lines.insert(at, line)
            end += 1
        elif overwrite:
            lines[found] = line
    return "\n".join(lines) + "\n"


def spotify_player_config(text, client_id):
    text = set_toml_keys(text, {"client_id": client_id,
                                "enable_media_control": False})  # the daemon can't run with it on macOS
    text = set_toml_keys(text, {"client_port": 18080,  # the default 8080 often clashes with dev servers
                                "enable_notify": False}, overwrite=False)
    if not re.search(r"(?m)^\s*\[device\]", text):
        text = text.rstrip("\n") + '\n\n[device]\nname = "herdr"\ndevice_type = "computer"\n'
    return text


def current_client_id():
    return parse_toml(spotify_player_config_path()).get("client_id", "")


def write_plugin_config(values):
    path = plugin_config_path()
    text = read_text(path) or ("# Now Playing for Spotify, written by setup. Every setting is described in\n"
                               "# config.example.toml in the plugin folder.\n")
    write_text(path, set_toml_keys(text, values))
    CONFIG.update(values)


def add_keybindings(text, bindings):
    """Append herdr [[keys.command]] blocks for plugin actions. Returns (text, actions bound, keys
    skipped because the user already uses them for something else)."""
    bound, skipped = set(), []
    for key, action, description in bindings:
        command = f"{PLUGIN_ID}.{action}"
        if re.search(rf'(?m)^\s*command\s*=\s*"{re.escape(command)}"', text):
            bound.add(action)
            continue
        if re.search(rf'(?m)^\s*key\s*=\s*"{re.escape(key)}"', text):
            skipped.append(key)
            continue
        block = f'[[keys.command]]\nkey = "{key}"\ntype = "plugin_action"\ncommand = "{command}"\ndescription = "{description}"\n'
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
        bound.add(action)
    return text, bound, skipped


def add_tab_bar(text, command, bottom=False):
    """Add a `nowplaying bar` command entry to herdr's [ui] tab_bar_right. Returns the new text, or
    None when an existing tab_bar_right is laid out in a way this can't edit safely."""
    entry = f'{{ type = "command", command = "{command}", interval_seconds = 5, timeout_seconds = 4 }}'
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.fullmatch(r"\s*\[ui\]\s*(#.*)?", line)), None)
    if start is None:
        lines += ([""] if lines and lines[-1].strip() else []) + ["[ui]"]
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    section = lines[start + 1:end]

    def find(key):
        return next((i for i, line in enumerate(section) if re.match(rf"\s*{key}\s*=", line)), None)

    if "nowplaying bar" not in text:
        i = find("tab_bar_right")
        if i is None:
            section[0:0] = ["tab_bar_right = [", f"  {entry},", "]"]
        elif re.fullmatch(r"\s*tab_bar_right\s*=\s*\[\s*", section[i]):
            section.insert(i + 1, f"  {entry},")
        elif m := re.fullmatch(r"(\s*tab_bar_right\s*=\s*\[)(.*)\]\s*", section[i]):
            inner = m.group(2).strip().rstrip(",")
            section[i] = f"{m.group(1)}{inner + ', ' if inner else ''}{entry}]"
        else:
            return None
    if find("hide_tab_bar_when_single_tab") is None:
        section.insert(0, "hide_tab_bar_when_single_tab = false")
    if bottom:
        i = find("tab_bar_position")
        if i is None:
            section.insert(0, 'tab_bar_position = "bottom"')
        else:
            section[i] = 'tab_bar_position = "bottom"'
    lines[start + 1:end] = section
    return "\n".join(lines) + "\n"


def launcher_script(herdr):
    return f"""#!/bin/sh
# {LAUNCHER_MARK} for the herdr plugin {PLUGIN_ID}, written by its setup.
# Finds the plugin's current install on every run, so plugin updates never leave it stale.
herdr="${{HERDR_BIN_PATH:-$(command -v herdr || echo {shlex.quote(herdr)})}}"
root=$("$herdr" plugin list --json 2>/dev/null | /usr/bin/env python3 -c '
import json, sys
for p in json.load(sys.stdin)["result"]["plugins"]:
    if p.get("plugin_id") == "{PLUGIN_ID}" and p.get("enabled", True):
        print(p["plugin_root"])
' 2>/dev/null)
if [ -z "$root" ]; then
    echo "nowplaying: the herdr plugin {PLUGIN_ID} isn't installed or enabled" >&2
    exit 1
fi
exec python3 "$root/nowplaying.py" "$@"
"""


def install_launcher():
    """Install the `nowplaying` command in ~/.local/bin. Returns (ok, note)."""
    path = home(".local", "bin", "nowplaying")
    if os.path.lexists(path):
        ours = os.path.islink(path) and os.readlink(path).endswith(os.path.join("bin", "nowplaying"))
        if not ours:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    ours = LAUNCHER_MARK in f.read(4000)
            except OSError:
                pass
        if not ours:
            return False, f"{path} already exists and isn't from this plugin, so setup left it alone."
        os.remove(path)
    herdr = herdr_bin()
    write_text(path, launcher_script(os.path.abspath(shutil.which(herdr) or herdr)), 0o755)
    on_path = os.path.dirname(path) in os.environ.get("PATH", "").split(os.pathsep)
    return True, "" if on_path else "Add ~/.local/bin to your PATH to run `nowplaying` in your shell."


def herdr_version():
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", run([herdr_bin(), "--version"]))
    return tuple(int(n) for n in m.groups()) if m else (0, 0, 0)


def herdr_config_issues():
    r = subprocess.run([herdr_bin(), "config", "check"], capture_output=True, text=True)
    return {line.strip() for line in (r.stdout + r.stderr).splitlines()
            if line.strip() and not line.startswith("config:")}


def configure_herdr(player_key, controls, tab_bar, bottom):
    """Edit herdr's config.toml (backed up first), validate with `herdr config check` and reload.
    Rolls back if herdr reports anything new. Returns (actions bound, notes)."""
    path = herdr_config_path()
    before = read_text(path)
    text, bound, skipped = add_keybindings(
        before, (KEYBINDINGS["player"] if player_key else []) + (KEYBINDINGS["controls"] if controls else []))
    notes = [f"{key} is already used in your herdr config, so setup didn't bind it." for key in skipped]
    if tab_bar:
        with_bar = add_tab_bar(text, BAR_COMMAND, bottom)
        if with_bar is None:
            notes.append("Couldn't add the tab bar: edit tab_bar_right yourself (see the README).")
        else:
            text = with_bar
    if text == before:
        return bound, notes
    issues_before = herdr_config_issues()
    backup = f"{path}.bak-nowplaying"
    if before:
        shutil.copy2(path, backup)
    write_text(path, text)
    new_issues = herdr_config_issues() - issues_before
    if new_issues:
        write_text(path, before) if before else os.remove(path)
        return set(), notes + [f"herdr rejected the change ({sorted(new_issues)[0]}), so your config is unchanged."]
    subprocess.run([herdr_bin(), "server", "reload-config"], capture_output=True)
    if before:
        notes.append(f"Your previous herdr config is saved as {backup.replace(home(), '~')}.")
    if tab_bar and bottom:
        notes.append("If the tab bar didn't move, choose reload config from herdr's menu.")
    return bound, notes


def copy_to_clipboard(text):
    try:
        return subprocess.run(["pbcopy"], input=text, text=True, capture_output=True).returncode == 0
    except OSError:
        return False


def find_cargo():
    cargo = shutil.which("cargo") or home(".cargo", "bin", "cargo")
    return cargo if os.access(cargo, os.X_OK) else None


class Setup:
    """The first-run setup screens. run() returns True when the user wants the player next."""

    def __init__(self, stdscr):
        self.scr = stdscr
        self.summary = []  # (ok, text) for the last screen

    # drawing

    def screen(self, title, lines, keys, notice="", progress=None):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        put(scr, 0, 2, "♫ Now Playing · setup", curses.color_pair(1) | curses.A_BOLD)
        put(scr, 2, 2, title, curses.A_BOLD)
        y = 4
        for line in lines:
            text, attr = line if isinstance(line, tuple) else (line, 0)
            indent = len(text) - len(text.lstrip())
            for part in textwrap.wrap(text, max(20, w - 4 - indent), subsequent_indent=" " * (indent + 3)) or [""]:
                if y < h - 3:
                    put(scr, y, 2, part, attr)
                y += 1
        for line in progress or []:
            if y < h - 3:
                put(scr, y, 4, line, curses.A_DIM)
            y += 1
        put(scr, h - 2, 2, keys, curses.A_DIM)
        if notice:
            put(scr, h - 1, 2, notice, curses.color_pair(2) | curses.A_BOLD)
        scr.refresh()

    def key(self):
        """Next key as a str ("\\n" for Enter, "esc" for Esc); ignores pasted text and escape sequences."""
        while True:
            k = self.scr.getch()
            if k == -1:
                continue
            if k == 27:
                seq = escape_sequence(self.scr, drop_paste=True)
                if seq is None:
                    return "esc"
                if seq in ("A", "B"):
                    return "up" if seq == "A" else "down"
                continue
            if k in (10, 13, curses.KEY_ENTER):
                return "\n"
            if k in (curses.KEY_UP, curses.KEY_DOWN):
                return "up" if k == curses.KEY_UP else "down"
            if 0 <= k < 256:
                return chr(k).lower()

    def ask(self, title, lines, keys, valid, notice=""):
        while True:
            self.screen(title, lines, keys, notice)
            k = self.key()
            if k in valid:
                return k

    def run_live(self, title, lines, argv, keys="[esc] cancel"):
        """Run a command, showing its last lines. Returns (returncode, tail)."""
        tail = deque(maxlen=6)
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, errors="replace")
        except OSError as e:
            return 1, [str(e)]
        reader = threading.Thread(target=lambda: [tail.append(l.rstrip()[:200]) for l in proc.stdout], daemon=True)
        reader.start()
        self.scr.timeout(250)
        try:
            while proc.poll() is None:
                self.screen(title, lines, keys, progress=list(tail))
                k = self.scr.getch()
                if k == 27 and lone_escape(self.scr, drop_paste=True):
                    proc.terminate()
        finally:
            self.scr.timeout(500)
        reader.join(2)
        return proc.wait(), list(tail)

    # steps

    def run(self):
        sp = os.access(spotify_player_bin(), os.X_OK)
        choice = self.ask("Welcome! Let's get you set up.", [
            "Which player should play your music?",
            "",
            ("  [1] spotify_player" + ("  (installed)" if sp else ""), curses.A_BOLD),
            "      Plays in the background; no Spotify app needed. Needs Spotify Premium.",
            "",
            ("  [2] Spotify desktop app", curses.A_BOLD),
            "      Controls the Spotify app you already use. Free or Premium.",
        ], "[1] [2] choose   [s] skip setup, I've set things up myself   [q] quit", "12sq")
        if choice == "q":
            return False
        if choice == "s":
            write_plugin_config({"backend": CONFIG["backend"]})
            return True
        if choice == "1":
            if not (self.install_step() and self.client_id_step() and self.login_step()):
                return False
            backend = "spotify_player"
        else:
            backend = "applescript"
            if not os.path.exists("/Applications/Spotify.app"):
                self.summary.append((False, "The Spotify app isn't in /Applications; install it from spotify.com."))
        if not self.extras_step(backend):
            return False
        return self.done_step(backend)

    def install_step(self):
        notice = ""
        while not os.access(spotify_player_bin(), os.X_OK):
            cargo = find_cargo()
            if not cargo:
                k = self.ask("Install Rust first", [
                    "spotify_player is built from source with Rust, and Rust isn't installed.",
                    f"Install it from {RUSTUP_URL} (one command in a terminal), then check again here.",
                ], "[w] open rustup.rs   [r] check again   [q] quit", "wrq", notice)
                if k == "w":
                    open_url(RUSTUP_URL)
                elif k == "q":
                    return False
                notice = "" if k == "w" else "Rust still isn't installed (no cargo found)."
                continue
            lines = ["spotify_player plays Spotify in the background. It isn't installed yet; setup can",
                     "build it with Rust's cargo. It takes about 5 minutes.",
                     "",
                     ("cargo " + " ".join(CARGO_INSTALL), curses.A_DIM)]
            k = self.ask("Install spotify_player", lines, "[enter] build it   [q] quit", "\nq", notice)
            if k == "q":
                return False
            code, tail = self.run_live("Building spotify_player… (about 5 minutes)", lines, [cargo, *CARGO_INSTALL])
            if code == 0:
                self.summary.append((True, "Built spotify_player."))
                continue
            hint = " If it mentions the lock file version, run `rustup update` and retry." \
                if any("lock file" in line for line in tail) else ""
            k = self.ask("The build didn't finish", ["Last output:", *[("  " + line, curses.A_DIM) for line in tail],
                                                     "", f"Fix the problem and retry.{hint}"],
                         "[r] retry   [q] quit", "rq")
            if k == "q":
                return False
            notice = ""
        return True

    def client_id_step(self):
        client_id = current_client_id()
        if client_id and client_id != NCSPOT_CLIENT_ID:
            return True
        copied = copy_to_clipboard(REDIRECT_URI)
        lines = [
            "Spotify asks every player to use its own free developer app. It takes about a minute:",
            "",
            "1. Press w to open the Spotify Developer Dashboard, log in and click Create app.",
            "2. App name and description: anything, for example “herdr”.",
            f"3. Redirect URI: {REDIRECT_URI}" + ("   (copied to your clipboard)" if copied else ""),
            "4. Under the APIs you'll use, tick Web API. Agree to the terms and save.",
            "5. Copy the Client ID (not the Client secret) and press enter to paste it here.",
        ]
        notice = ""
        while True:
            k = self.ask("Create your Spotify app", lines, "[w] open the dashboard   [enter] paste Client ID   [q] quit",
                         "w\nq", notice)
            if k == "w":
                open_url(DASHBOARD_URL)
                continue
            if k == "q":
                return False
            self.screen("Create your Spotify app", lines, "[enter] save   [esc] back")
            entered = read_query(self.scr, "Client ID: ")
            if entered is None:
                continue
            entered = re.sub(r"\s", "", entered)
            if not re.fullmatch(r"[0-9a-fA-F]{32}", entered):
                notice = "That isn't a Client ID (it's 32 letters and digits). Try again."
                continue
            path = spotify_player_config_path()
            write_text(path, spotify_player_config(read_text(path), entered.lower()))
            # A running daemon still has the old settings; the login step starts it again.
            subprocess.run(["pkill", "-f", "spotify_player -d"], capture_output=True)
            self.summary.append((True, f"Saved your Client ID to {path.replace(home(), '~')}."))
            return True

    def login_step(self):
        backend = SpotifyPlayerBackend(spotify_player_bin())
        if not backend.missing_logins():
            backend.start()
            return True
        lines = ["Your browser opens up to three Spotify pages, one after another. Approve each:",
                 "",
                 "1. Your app (the one you just created): playback, search and controls.",
                 "2. ncspot: spotify_player's backup for a few features Spotify restricts for new",
                 "   personal apps, such as reading your playlists. This is expected.",
                 "3. Spotify: the streaming login that plays the audio.",
                 "",
                 ("Pages you've approved before finish on their own.", curses.A_DIM)]
        notice = ""
        while True:
            k = self.ask("Log in to Spotify", lines, "[enter] log in   [q] quit", "\nq", notice)
            if k == "q":
                return False
            state = {"url": "", "ok": None}

            def job():
                state["ok"] = backend.login(on_url=lambda url: state.update(url=url))

            threading.Thread(target=job, daemon=True).start()
            self.scr.timeout(250)
            try:
                while state["ok"] is None:
                    url = state["url"]
                    self.screen("Approve the login in your browser…", [
                        "Already-approved steps finish on their own; one tab may open per step.",
                        "",
                        *(["If no tab opened, press w or visit:", (url, curses.A_DIM)] if url else []),
                    ], "[w] open the login page again   [esc] cancel")
                    k = self.scr.getch()
                    if k == ord("w") and url:
                        open_url(url)
                    elif k == 27 and lone_escape(self.scr, drop_paste=True):
                        backend.cancel_login()
            finally:
                self.scr.timeout(500)
            if state["ok"]:
                self.summary.append((True, "Logged in to Spotify; spotify_player is running."))
                return True
            notice = "The login didn't finish. Try again."

    def extras_step(self, backend):
        herdr_text = read_text(herdr_config_path())
        new_herdr = herdr_version() >= (0, 9, 0)

        def has(command):
            return f'"{PLUGIN_ID}.{command}"' in herdr_text

        options = [  # id, label, on, available
            ["autostart", "Start spotify_player when herdr starts", True, backend == "spotify_player"],
            ["player_key", "prefix m opens the player" + ("  (already set)" if has("open-popup") else ""), True, True],
            ["controls", "prefix , / space / . for previous / play-pause / next"
             + ("  (already set)" if has("play-pause") else ""), True, True],
            ["tab_bar", "Show what's playing in herdr's tab bar" + ("  (already set)" if "nowplaying bar" in herdr_text
                                                                    else "" if new_herdr else "  (needs herdr 0.9.0+)"),
             new_herdr, new_herdr],
            ["bottom", "Put the tab bar at the bottom of the screen", False, new_herdr],
        ]
        options = [o for o in options if o[3]]
        sel = 0
        while True:
            lines = ["Pick what to set up (prefix is your herdr prefix key, ctrl+b unless you changed it).",
                     "Setup also installs the `nowplaying` command in ~/.local/bin.", ""]
            for i, (_, label, on, _) in enumerate(options):
                lines.append((f"{'›' if i == sel else ' '} [{'x' if on else ' '}] {label}",
                              curses.A_BOLD if i == sel else 0))
            self.screen("Extras", lines, "↑/↓ move   space toggle   enter apply   q quit")
            k = self.key()
            if k in ("up", "k"):
                sel = (sel - 1) % len(options)
            elif k in ("down", "j"):
                sel = (sel + 1) % len(options)
            elif k == " ":
                options[sel][2] = not options[sel][2]
            elif k == "q":
                return False
            elif k == "\n":
                break
        chosen = {o[0]: o[2] for o in options}

        self.screen("Applying…", [], "")
        ok, note = install_launcher()
        self.summary.append((ok, "Installed the `nowplaying` command." if ok else note))
        if ok and note:
            self.summary.append((False, note))
        bound, notes = configure_herdr(chosen.get("player_key", False), chosen.get("controls", False),
                                       chosen.get("tab_bar", False), chosen.get("bottom", False))
        if "open-popup" in bound:
            self.summary.append((True, "prefix m opens the player."))
        if bound & set(BAR_KEYS):
            self.summary.append((True, "prefix , / space / . control playback."))
        if chosen.get("tab_bar") and not any("tab bar" in n or "rejected" in n for n in notes):
            self.summary.append((True, "What's playing shows in herdr's tab bar."))
        self.summary += [(False, n) for n in notes]
        values = {"backend": backend, "autostart_player": chosen.get("autostart", False),
                  "bar_controls": bool(bound & set(BAR_KEYS))}
        for action, key in BAR_KEYS.items():  # hint only keys that are really bound
            values[key] = DEFAULTS[key] if action in bound else ""
        write_plugin_config(values)
        return True

    def done_step(self, backend):
        lines = [(("✓ " if ok else "! ") + text, 0 if ok else curses.color_pair(2)) for ok, text in self.summary]
        if backend == "spotify_player":
            lines += ["", "To start music: open the player and press / to search, or t to move what's",
                      "playing on another device (your phone, the Spotify app) to “herdr”."]
        k = self.ask("You're all set", lines, "[enter] open the player   [q] close", "\nq")
        return k == "\n"


# --- CLI ---------------------------------------------------------------------------------------

USAGE = """usage: nowplaying [command]

  (none)          open the player (herdr split pane; plain terminal outside herdr)
  popup           open the player as a herdr popup
  play | pause    toggle play/pause
  next | prev     skip track
  here            move playback to the spotify_player device
  now             print what's playing
  web             open the current track in Spotify
  bar             one-line status for herdr's tab bar (ui.tab_bar_right)
  search [--play] <query>
                  search Spotify (spotify_player); --play starts the top track
  login           log in to Spotify (opens your browser), then start the daemon
  daemon          start the spotify_player daemon if it isn't running
  setup           set everything up step by step (also runs the first time you open the player)"""


def run_with_recovery(backend, do):
    """CLI/action path: run a command, and if Spotify answered 404 (it lost our Connect device),
    reconnect the device and run it once more."""
    sent = time.time()
    do()
    if not getattr(backend, "restart", None):
        return
    time.sleep(0.6)  # the daemon logs a failed request shortly after the CLI returns
    if 404 in backend.request_errors_since(sent):
        print("Spotify lost the player device; reconnecting…", file=sys.stderr)
        if backend.restart():
            do()
        else:
            sys.exit("Couldn't reconnect the player device. Try again in a minute.")


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
    if needs_setup() and entrypoint == "player":
        entrypoint = "setup"  # first run: the setup popup is bigger than the player's
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
    elif cmd == "login":
        backend = pick_backend()
        if not backend.login:
            sys.exit("The Spotify app backend logs in through the Spotify app itself.")
        print("Opening your browser to log in to Spotify (one tab per login step)…")
        if not backend.login(on_url=lambda url: print(f"If no tab opened, visit:\n  {url}")):
            sys.exit("Login didn't finish.")
        print("Logged in. spotify_player is running.")
    elif cmd == "daemon":
        backend = pick_backend()
        if backend.missing_logins():
            sys.exit("Not logged in to Spotify yet. Run: nowplaying login")
        backend.start()
    elif cmd in ("setup", "setup-ui"):
        if cmd == "setup" and os.environ.get("HERDR_ENV") and not sys.stdin.isatty():
            # Invoked as a plugin action: there's no terminal here, so show setup in a popup.
            r = subprocess.run([herdr_bin(), "plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "setup"],
                               capture_output=True, text=True)
            if r.returncode:
                sys.exit(r.stderr.strip() or r.stdout.strip())
        else:
            curses.wrapper(ui, True)
    elif cmd == "startup":  # herdr [[startup]] hook: one-shot
        if needs_setup():
            run([herdr_bin(), "notification", "show", "Now Playing: finish setting up",
                 "--body", f"Run: herdr plugin action invoke {PLUGIN_ID}.setup", "--sound", "none"])
        elif CONFIG["autostart_player"]:
            backend = pick_backend()
            if backend.missing_logins():
                run([herdr_bin(), "notification", "show", "Now Playing: not logged in to Spotify",
                     "--body", "Open the player and press a, or run: nowplaying login", "--sound", "none"])
            else:
                backend.start()
    elif cmd == "bar":
        print(bar_line(pick_backend().status(heal=False)))
    elif cmd in ("now", "web"):
        s = pick_backend().status()
        if s["state"] not in ("playing", "paused"):
            print({"NO_TRACK": "nothing playing",
                   "NOT_AUTHENTICATED": "not logged in to Spotify. Run: nowplaying login"}.get(s["state"], "player not running"))
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
            run_with_recovery(backend, lambda: backend.play_item(tracks[0]))
            print(f"▶ {tracks[0]['title']} — {tracks[0]['sub']}")
            return
        for name in ("Tracks", "Albums", "Artists", "Playlists"):
            items = results.get(name, [])[:5]
            if items:
                print(name)
                for item in items:
                    print(f"  {item['title']}" + (f"  —  {item['sub']}" if item["sub"] else ""))
    elif cmd in ("playpause", "next", "previous", "transfer"):
        backend = pick_backend()
        run_with_recovery(backend, lambda: backend.command(cmd))
    else:
        sys.exit(f"unknown command: {cmd}\n\n{USAGE}")


if __name__ == "__main__":
    main()

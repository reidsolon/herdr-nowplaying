"""Regenerate docs/player.png and docs/search.png from the real UI with fictional demo data.

Dev-only (not used by the plugin):  pip install pyte pillow fonttools
Usage:  python docs/render_screenshots.py . docs
"""
import os, pty, sys, time, select, json, struct, fcntl, termios
import pyte, pyte.graphics
from PIL import Image, ImageDraw, ImageFont
from fontTools.ttLib import TTCollection

REPO, OUT = sys.argv[1], sys.argv[2]
COLS, ROWS = 66, 16
pyte.graphics.TEXT[2] = "+italics"   # pyte ignores SGR 2 (dim); carry it as italics, drawn dim below

SCENES = {
    "player": [],
    "search": ["/", "sleep:0.3", "night drive", "sleep:0.3", "\r", "sleep:1.2"],
}

def child():
    os.environ["TERM"] = "xterm-256color"
    for k in ("HERDR_PANE_ID", "HERDR_ENV"): os.environ.pop(k, None)
    sys.path.insert(0, REPO)
    import nowplaying as n, curses
    TRACKS = [("Night Drive", "The Lantern Club", "Neon Weather"), ("Night Drive (Slowed)", "Halcyon Wire", "After Hours"),
              ("Drive All Night", "Maple & Ash", "Long Roads"), ("Nightdriving", "Kite Season", "Signal Fires"),
              ("Night Drive Home", "Paper Planets", "Late Static"), ("Drive", "Northbound", "Mile Markers")]
    class Demo:
        name = "spotify_player"; poll_seconds = 60; device_name = "herdr"
        def status(self):
            return {"state": "playing", "name": "Midnight Static", "artist": "The Lantern Club", "album": "Neon Weather",
                    "duration": 238, "position": 102, "volume": 70, "shuffle": True, "repeat": "context",
                    "device": "herdr", "url": "", "id": "demo", "context": ""}
        def search(self, q):
            return {"Tracks": [{"kind": "track", "id": str(i), "title": t, "sub": f"{a} · {al}"} for i, (t, a, al) in enumerate(TRACKS)],
                    "Albums": [{"kind": "album", "id": "a", "title": "Neon Weather", "sub": "The Lantern Club · 2025"}] * 4,
                    "Artists": [{"kind": "artist", "id": "r", "title": "The Lantern Club", "sub": ""}] * 3,
                    "Playlists": [{"kind": "playlist", "id": "p", "title": "Night Drive Mix", "sub": ""}] * 5}
        def command(self, *a): pass
        def missing_logins(self): return []
        rate_limited_since = lambda self, s: False
    n.pick_backend = lambda: Demo()
    curses.wrapper(n.ui)

def capture(keys):
    pid, fd = pty.fork()
    if pid == 0:
        child(); os._exit(0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    screen = pyte.Screen(COLS, ROWS); stream = pyte.ByteStream(screen)
    def pump(t):
        end = time.time() + t
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                try: stream.feed(os.read(fd, 65536))
                except OSError: return
    pump(1.5)
    for k in keys:
        if k.startswith("sleep:"): pump(float(k[6:])); continue
        for ch in k: os.write(fd, ch.encode()); pump(0.02)
    pump(0.6)
    snap = [[screen.buffer[y][x] for x in range(COLS)] for y in range(ROWS)]
    os.write(fd, b"q"); pump(0.3)
    return snap

# --- drawing --------------------------------------------------------------------------------
SCALE = 2
FS = 15 * SCALE
menlo = TTCollection("/System/Library/Fonts/Menlo.ttc")
cmaps = [f.getBestCmap() for f in menlo.fonts]
F = {i: ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", FS, index=i) for i in (0, 1)}
CW = F[0].getlength("M"); LH = int(FS * 1.45)
BG, FG = (30, 33, 40), (191, 197, 208)
COLORS = {"green": (152, 195, 121), "yellow": (229, 192, 123), "default": FG}
PAD, BAR = 28 * SCALE, 34 * SCALE

def font_for(ch, bold):
    if bold and ord(ch) in cmaps[1]: return F[1]
    return F[0]

def draw(snap, path, title):
    while len(snap) > 1 and not "".join(c.data for c in snap[-1]).strip() and not "".join(c.data for c in snap[-2]).strip():
        snap = snap[:-1]  # crop trailing blank rows, keep one for breathing room
    rows = len(snap)
    W = int(PAD * 2 + CW * COLS); H = int(BAR + PAD * 1.4 + LH * rows)
    margin = 40 * SCALE
    img = Image.new("RGB", (W + margin * 2, H + margin * 2), (14, 16, 20))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([margin, margin, margin + W, margin + H], radius=12 * SCALE, fill=BG, outline=(52, 57, 68), width=SCALE)
    for i, c in enumerate([(237, 106, 94), (245, 191, 79), (98, 197, 84)]):
        cx, cy = margin + 20 * SCALE + i * 20 * SCALE, margin + BAR // 2
        d.ellipse([cx - 6 * SCALE, cy - 6 * SCALE, cx + 6 * SCALE, cy + 6 * SCALE], fill=c)
    tw = F[0].getlength(title) * 0.8
    d.text((margin + (W - tw) / 2, margin + BAR / 2), title, font=ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", int(FS * 0.8)), fill=(130, 137, 150), anchor="lm")
    ox, oy = margin + PAD, margin + BAR + PAD * 0.5
    for y, row in enumerate(snap):
        for x, c in enumerate(row):
            fg = COLORS.get(c.fg, FG)
            bg = None
            if c.reverse: fg, bg = BG, FG
            if c.italics:  # dim
                fg = tuple(int(a * 0.55 + b * 0.45) for a, b in zip(fg, BG))
            px, py = ox + x * CW, oy + y * LH
            if bg: d.rectangle([px, py, px + CW + 1, py + LH], fill=bg)
            if c.data in "━─":  # draw bar segments as solid lines so they join without seams
                t = 3 * SCALE if c.data == "━" else 1 * SCALE
                d.rectangle([px, py + LH / 2 - t / 2, px + CW + 0.5, py + LH / 2 + t / 2], fill=fg)
            elif c.data.strip():
                d.text((px, py + LH / 2), c.data, font=font_for(c.data, c.bold), fill=fg, anchor="lm")
    img.save(path, optimize=True)

for name, keys in SCENES.items():
    snap = capture(keys)
    draw(snap, os.path.join(OUT, f"{name}.png"), "herdr · Now Playing")
    print(name, "->", os.path.join(OUT, f"{name}.png"))
    print("\n".join("".join(c.data for c in row).rstrip() for row in snap))

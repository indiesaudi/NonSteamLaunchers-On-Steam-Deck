#!/usr/bin/env python3
"""Open every NSL launcher shortcut from Game Mode and check it comes up.

Launches each Steam shortcut through the Steam client itself (SteamClient.Apps.RunGame over the
CEF DevTools port, the same path as pressing Play), waits, then records whether the launcher's
process is running and saves a gamescope screenshot of what is on screen. Stops it again through
Steam before moving on. Run on the device while it is in Game Mode.

Usage: nsl_arm64_gamemode_test.py [shortcut name ...]   (default: every known launcher shortcut)
Env:   LAUNCH_WAIT (s, default 75)
"""

import base64
import ctypes
import ctypes.util
import glob
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

LAUNCH_WAIT = int(os.environ.get("LAUNCH_WAIT", "75"))
HOME = os.path.expanduser("~")
STEAM = f"{HOME}/.local/share/Steam"
PREFIX = f"{STEAM}/steamapps/compatdata/NonSteamLaunchers"
OUT = f"{HOME}/Downloads/nsl-arm64-gamemode/{time.strftime('%Y%m%d-%H%M%S')}"

LAUNCHERS = [
    "Epic Games",
    "GOG Galaxy",
    "Ubisoft Connect",
    "Battle.net",
    "Amazon Games",
    "EA App",
    "itch.io",
    "Legacy Games",
    "Humble Bundle",
    "IndieGala Client",
    "Rockstar Games Launcher",
    "Glyph",
    "Minecraft Launcher",
    "Playstation Plus",
    "VK Play",
    "HoYoPlay",
    "Nexon Launcher",
    "Game Jolt Client",
    "Artix Game Launcher",
    "ARC Launcher",
    "Pokémon Trading Card Game Live",
    "Antstream Arcade",
    "PURPLE Launcher",
    "Plarium Play",
    "VFUN Launcher",
    "Tempo Launcher",
    "STOVE Client",
    "Big Fish Games Manager",
    "Gryphlink",
    "Heroic Games Launcher",
    "Prism Launcher",
]


def read_shortcuts():
    """Parse Steam's binary shortcuts.vdf into {name: shortcut dict}."""
    path = glob.glob(f"{STEAM}/userdata/*/config/shortcuts.vdf")[0]
    data = open(path, "rb").read()

    def parse(i):
        obj = {}
        while True:
            kind = data[i]
            i += 1
            if kind == 8:
                return obj, i
            end = data.index(b"\0", i)
            key = data[i:end].decode("utf-8", "replace").lower()
            i = end + 1
            if kind == 0:
                value, i = parse(i)
            elif kind == 1:
                end = data.index(b"\0", i)
                value = data[i:end].decode("utf-8", "replace")
                i = end + 1
            else:
                value = struct.unpack("<I", data[i : i + 4])[0]
                i += 4
            obj[key] = value

    root, _ = parse(0)
    return {s["appname"]: s for s in root["shortcuts"].values()}


class Cdp:
    """Minimal Chrome DevTools client for Steam's SharedJSContext."""

    def __init__(self, title="SharedJSContext"):
        targets = json.load(urllib.request.urlopen("http://127.0.0.1:8080/json"))
        url = urllib.parse.urlparse(next(t["webSocketDebuggerUrl"] for t in targets if t["title"] == title))
        self.sock = socket.create_connection((url.hostname, url.port), timeout=120)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET {url.path} HTTP/1.1\r\nHost: {url.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.sock.recv(4096)
        self.buf = self.buf.split(b"\r\n\r\n", 1)[1]
        self.next_id = 0

    def _need(self, n):
        while len(self.buf) < n:
            self.buf += self.sock.recv(65536)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def eval(self, expr):
        self.next_id += 1
        payload = json.dumps(
            {
                "id": self.next_id,
                "method": "Runtime.evaluate",
                "params": {"expression": expr, "awaitPromise": True, "returnByValue": True},
            }
        ).encode()
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        while True:
            h = self._need(2)
            ln = h[1] & 127
            if ln == 126:
                ln = struct.unpack(">H", self._need(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._need(8))[0]
            msg = json.loads(self._need(ln))
            if msg.get("id") == self.next_id:
                return msg.get("result", {}).get("result", {}).get("value")


def game_id(appid):
    # Non-Steam shortcuts run as the 64-bit game id (appid << 32) | 0x02000000
    return str((appid << 32) | 0x02000000)


def process_running(shortcut):
    """True when the shortcut's target is running: a Windows exe in the prefix, a flatpak, or a native binary."""
    # The target may carry arguments after the quoted path (Epic's is "...EpicGamesLauncher.exe" -opengl)
    raw = shortcut.get("exe", "")
    quoted = re.match(r'"([^"]*)"', raw)
    exe = quoted.group(1) if quoted else raw
    opts = shortcut.get("launchoptions", "")
    if exe.lower().endswith(".exe"):
        pattern = r"^[A-Za-z]:[/\\].*" + re.escape(os.path.basename(exe))
    elif exe.endswith("/flatpak"):
        app = re.search(r"run (\S+)", opts)
        pattern = re.escape(app.group(1)) if app else "flatpak"
    else:
        pattern = re.escape(os.path.basename(exe))
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def keep_awake():
    """Nudge the pointer and tap Shift so Game Mode's screensaver doesn't cover the launcher."""
    x = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11"))
    xt = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xtst"))
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XKeysymToKeycode.restype = ctypes.c_ubyte
    x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x.XCloseDisplay.argtypes = [ctypes.c_void_p]
    xt.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    xt.XTestFakeRelativeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
    d = x.XOpenDisplay(os.environ.get("DISPLAY", ":0").encode())
    if not d:
        return
    xt.XTestFakeRelativeMotionEvent(d, 1, 0, 0)
    xt.XTestFakeRelativeMotionEvent(d, -1, 0, 0)
    shift = x.XKeysymToKeycode(d, 0xFFE1)
    xt.XTestFakeKeyEvent(d, shift, 1, 0)
    xt.XTestFakeKeyEvent(d, shift, 0, 0)
    x.XSync(d, 0)
    x.XCloseDisplay(d)


def wait_awake(seconds):
    end = time.time() + seconds
    while time.time() < end:
        keep_awake()
        time.sleep(min(20, max(0, end - time.time())))
    keep_awake()
    time.sleep(2)


def screenshot(dest):
    shot = "/tmp/gamescope.png"
    if os.path.exists(shot):
        os.remove(shot)
    subprocess.run(
        ["xprop", "-root", "-f", "GAMESCOPECTRL_REQUEST_SCREENSHOT", "32c", "-set", "GAMESCOPECTRL_REQUEST_SCREENSHOT", "1"],
        env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
        capture_output=True,
    )
    for _ in range(20):
        if os.path.exists(shot) and os.path.getsize(shot) > 0:
            time.sleep(1)
            shutil.copy(shot, dest)
            return True
        time.sleep(0.5)
    return False


def stop(cdp, gid):
    cdp.eval(f"SteamClient.Apps.TerminateApp('{gid}', false)")
    time.sleep(8)
    # Whatever Steam left behind (launcher helpers, wineserver) so the next launch starts clean
    subprocess.run(["pkill", "-TERM", "-f", r"^[A-Za-z]:[/\\]"], capture_output=True)
    time.sleep(4)
    subprocess.run(["pkill", "-KILL", "-f", r"^[A-Za-z]:[/\\]"], capture_output=True)
    subprocess.run(["pkill", "-KILL", "-f", "wineserver"], capture_output=True)
    time.sleep(3)


def main():
    os.makedirs(OUT, exist_ok=True)
    names = sys.argv[1:] or LAUNCHERS
    shortcuts = read_shortcuts()
    cdp = Cdp()
    rows = []
    for name in names:
        shortcut = shortcuts.get(name)
        if not shortcut:
            rows.append((name, "no shortcut", "-", "-"))
            print(f"{name}: no shortcut", flush=True)
            continue
        gid = game_id(shortcut["appid"])
        slug = re.sub(r"[^A-Za-z0-9]", "_", name)
        print(f"{name}: launching {gid} for {LAUNCH_WAIT}s", flush=True)
        cdp.eval(f"SteamClient.Apps.RunGame('{gid}', '', -1, 100)")
        wait_awake(LAUNCH_WAIT)
        running = cdp.eval(f"(window.SteamUIStore?.RunningApps || []).some(a => String(a.appid) === '{shortcut['appid']}')")
        alive = process_running(shortcut)
        shot = f"{slug}.png" if screenshot(f"{OUT}/{slug}.png") else "-"
        result = "PASS" if alive else "FAIL(not running)"
        rows.append((name, result, "yes" if running else "no", shot))
        print(f"{name}: {result} steam_running={running} screenshot={shot}", flush=True)
        stop(cdp, gid)

    with open(f"{OUT}/report.md", "w") as f:
        f.write(f"# NSL arm64 Game Mode launch test {time.strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write(f"Launched through Steam (SteamClient.Apps.RunGame), checked after {LAUNCH_WAIT}s.\n\n")
        f.write("| Shortcut | Result | Steam shows running | Screenshot |\n|---|---|---|---|\n")
        for row in rows:
            f.write("| " + " | ".join(row) + " |\n")
    open(f"{OUT}/DONE", "w").close()
    print(f"Report: {OUT}/report.md")


if __name__ == "__main__":
    main()

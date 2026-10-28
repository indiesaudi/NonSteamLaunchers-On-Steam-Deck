#!/usr/bin/env python3
"""Open every NSL launcher shortcut from Game Mode and check it comes up.

Launches each Steam shortcut through the Steam client itself (SteamClient.Apps.RunGame over the
CEF DevTools port, the same path as pressing Play), waits for gamescope to see its window, then records whether the launcher's
process is running and saves a gamescope screenshot of what is on screen. Stops it again through
Steam before moving on. Run on the device while it is in Game Mode.

Usage: nsl_arm64_gamemode_test.py [shortcut name ...]   (default: every known launcher shortcut)
Env:   LAUNCH_WAIT (max s to wait for a window, default 240), WINDOW_SETTLE (s to let it paint, default 20)
"""

import base64
import contextlib
import ctypes
import ctypes.util
import glob
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

LAUNCH_WAIT = int(os.environ.get("LAUNCH_WAIT", "240"))
# Time to let a window paint after gamescope first sees it (launchers often map a blank window first)
WINDOW_SETTLE = int(os.environ.get("WINDOW_SETTLE", "20"))
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
    with open(path, "rb") as f:
        data = f.read()

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
        with urllib.request.urlopen("http://127.0.0.1:8080/json") as resp:
            targets = json.load(resp)
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


class XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("c_class", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


def xlib():
    x = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11"))
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XDefaultRootWindow.restype = ctypes.c_ulong
    x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x.XQueryTree.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
        ctypes.POINTER(ctypes.c_uint),
    ]
    x.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XWindowAttributes)]
    x.XInternAtom.restype = ctypes.c_ulong
    x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x.XGetWindowProperty.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_long,
        ctypes.c_long,
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    x.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    x.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x.XKeysymToKeycode.restype = ctypes.c_ubyte
    x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x.XCloseDisplay.argtypes = [ctypes.c_void_p]
    return x


def window_text(x, d, w, prop):
    atom_type, fmt, count, after, data = ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p()
    atom = x.XInternAtom(d, prop.encode(), 0)
    if (
        x.XGetWindowProperty(d, w, atom, 0, 256, 0, 0, *(ctypes.byref(v) for v in (atom_type, fmt, count, after, data)))
        or not count.value
    ):
        return ""
    return ctypes.string_at(data, count.value).replace(b"\0", b" ").decode(errors="replace").strip()


def dismiss_dialogs(appid, display=":1"):
    """Press Enter on the app's popups; returns their titles.

    Covers first-run prompts Wine draws as separate windows: ones clearly smaller than the app's main window, or,
    before any main window exists, the topmost short window (VK Play's install folder picker, PlayStation Plus's VM
    warning). Splash screens ignore the key.
    """
    x = xlib()
    xt = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xtst"))
    xt.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    d = x.XOpenDisplay(display.encode())
    if not d:
        return []
    root = x.XDefaultRootWindow(d)
    parent, children, count = ctypes.c_ulong(), ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint()
    x.XQueryTree(d, root, ctypes.byref(ctypes.c_ulong()), ctypes.byref(parent), ctypes.byref(children), ctypes.byref(count))
    windows = []
    for i in range(count.value):
        w = children[i]
        attrs = XWindowAttributes()
        x.XGetWindowAttributes(d, w, ctypes.byref(attrs))
        if attrs.map_state == 2 and not attrs.override_redirect and f"steam_app_{appid}" in window_text(x, d, w, "WM_CLASS"):
            windows.append((attrs.width, attrs.height, w, window_text(x, d, w, "WM_NAME")))
    # XQueryTree lists windows bottom to top
    largest = max((width * height for width, height, _, _ in windows), default=0)
    if largest >= 400 * 600:
        popups = [w for w in windows if w[0] * w[1] < largest * 0.6]
    else:
        popups = [w for w in windows if w[1] < 300 and w[0] < 720][-1:]
    titles = []
    enter = x.XKeysymToKeycode(d, 0xFF0D)
    for _, _, w, title in popups:
        x.XRaiseWindow(d, w)
        x.XSetInputFocus(d, w, 2, 0)
        x.XSync(d, 0)
        time.sleep(0.5)
        xt.XTestFakeKeyEvent(d, enter, 1, 0)
        xt.XTestFakeKeyEvent(d, enter, 0, 0)
        x.XSync(d, 0)
        titles.append(title or hex(w))
    x.XCloseDisplay(d)
    return titles


STEAM_UI_APPID = "769"


def root_cardinals(prop):
    out = subprocess.run(
        ["xprop", "-root", prop],
        env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
        capture_output=True,
        text=True,
    ).stdout
    return re.findall(r"\d+", out.split("=", 1)[1]) if "=" in out else []


def window_shown(appid):
    """True once the app's window is on screen: gamescope has a window for it and Steam layers it above its own UI.

    GAMESCOPE_FOCUSED_APP stays 769 (Steam) for apps on the second Xwayland, so it can't be used here.
    """
    appid = str(appid)
    layers = root_cardinals("GAMESCOPECTRL_BASELAYER_APPID")
    if appid not in root_cardinals("GAMESCOPE_FOCUSABLE_APPS") or appid not in layers:
        return False
    return STEAM_UI_APPID not in layers or layers.index(appid) < layers.index(STEAM_UI_APPID)


def wait_for_window(appid, seconds):
    """Wait up to `seconds` for the app's window, then let it paint; returns seconds until it appeared, or None."""
    start = time.time()
    seen = None
    while time.time() < start + seconds:
        keep_awake()
        if window_shown(appid):
            seen = int(time.time() - start)
            time.sleep(WINDOW_SETTLE)
            break
        time.sleep(5)
    keep_awake()
    time.sleep(3)
    return seen if window_shown(appid) else None


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


def prefix_pids(appid, chain=True):
    """Wine processes (and the wineserver) in the NSL prefix, plus (with chain) Steam's launch chain for this app.

    Other games' prefixes and launches are left alone.
    """
    launch = f"SteamLaunch AppId={appid} ".encode() if chain else None
    pids = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read()
            with open(f"/proc/{pid}/environ", "rb") as f:
                env = f.read()
        except OSError:
            continue
        cmd = args.split(b"\0")[0].decode(errors="replace")
        in_prefix = (re.match(r"[A-Za-z]:[/\\]", cmd) or cmd.endswith("wineserver")) and PREFIX.encode() in env
        if in_prefix or (launch and launch in args.replace(b"\0", b" ")):
            pids.append(int(pid))
    return pids


def steam_running(cdp, appid):
    return cdp.eval(f"(window.SteamUIStore?.RunningApps || []).some(a => String(a.appid) === '{appid}')")


def wait_stopped(cdp, appid, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if not steam_running(cdp, appid):
            return True
        time.sleep(3)
    return False


def kill(pids, sig):
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, sig)


def stop(cdp, gid):
    # Let Steam end the app first. Never SIGKILL a native app Steam preloads its overlay into
    # (Heroic): killing it mid-IPC leaves Steam's SysMgrMutex held and freezes the whole Steam UI
    appid = str(int(gid) >> 32)
    cdp.eval(f"SteamClient.Apps.TerminateApp('{gid}', false)")
    if not wait_stopped(cdp, appid, 30):
        # Whatever Steam left behind (launcher helpers, wineserver, the launch wrapper Steam waits on)
        kill(prefix_pids(appid), signal.SIGTERM)
        wait_stopped(cdp, appid, 30)
    # Wine processes in the NSL prefix don't talk to Steam directly, so they can go hard
    kill(prefix_pids(appid, chain=False), signal.SIGKILL)
    # Steam refuses the next launch ("Game already running") until it has noticed this one exit
    wait_stopped(cdp, appid, 60)
    time.sleep(3)


def main():
    os.makedirs(OUT, exist_ok=True)
    names = sys.argv[1:] or LAUNCHERS
    shortcuts = read_shortcuts()
    cdp = Cdp()
    # Launching on top of another game (or another test's game) confuses both, and the input sent
    # to keep the screen awake would go to it
    busy = cdp.eval("JSON.stringify((window.SteamUIStore?.RunningApps || []).map(a => a.display_name))")
    if busy and busy != "[]":
        sys.exit(f"Steam is already running {busy}; close it first")
    rows = []
    for name in names:
        shortcut = shortcuts.get(name)
        if not shortcut:
            rows.append((name, "no shortcut", "-", "-", "-", "-"))
            print(f"{name}: no shortcut", flush=True)
            continue
        gid = game_id(shortcut["appid"])
        slug = re.sub(r"[^A-Za-z0-9]", "_", name)
        print(f"{name}: launching {gid}, waiting up to {LAUNCH_WAIT}s for a window", flush=True)
        is_running = f"(window.SteamUIStore?.RunningApps || []).some(a => String(a.appid) === '{shortcut['appid']}')"
        # Steam sometimes drops a launch requested just after the previous app exited; ask again
        for _ in range(3):
            cdp.eval(f"SteamClient.Apps.RunGame('{gid}', '', -1, 100)")
            time.sleep(15)
            if cdp.eval(is_running) or process_running(shortcut):
                break
        seen = wait_for_window(shortcut["appid"], LAUNCH_WAIT)
        shown = seen is not None
        took = f"{seen}s" if shown else "-"
        # Click through first-run popups so the screenshot shows the launcher itself
        dismissed = []
        for _ in range(3):
            titles = dismiss_dialogs(shortcut["appid"])
            if not titles:
                break
            dismissed += titles
            time.sleep(8)
        if dismissed:
            print(f"{name}: dismissed {dismissed}", flush=True)
            keep_awake()
            time.sleep(3)
            shown = shown or window_shown(shortcut["appid"])
        running = cdp.eval(is_running)
        alive = process_running(shortcut)
        shot = f"{slug}.png" if screenshot(f"{OUT}/{slug}.png") else "-"
        if alive and shown:
            result = "PASS"
        elif alive:
            result = "FAIL(no window)"
        else:
            result = "FAIL(not running)"
        rows.append((name, result, took, "; ".join(dismissed) or "-", "yes" if running else "no", shot))
        print(f"{name}: {result} window_after={took} steam_running={running} screenshot={shot}", flush=True)
        stop(cdp, gid)

    with open(f"{OUT}/report.md", "w") as f:
        f.write(f"# NSL arm64 Game Mode launch test {time.strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write(f"Launched through Steam (SteamClient.Apps.RunGame), waiting up to {LAUNCH_WAIT}s for a window.\n\n")
        f.write(
            "| Shortcut | Result | Window after | Popups dismissed | Steam shows running | Screenshot |\n|---|---|---|---|---|---|\n"
        )
        for row in rows:
            f.write("| " + " | ".join(row) + " |\n")
    with open(f"{OUT}/DONE", "w"):
        pass
    print(f"Report: {OUT}/report.md")


if __name__ == "__main__":
    main()

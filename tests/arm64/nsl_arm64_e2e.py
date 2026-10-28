#!/usr/bin/env python3
"""End-to-end NSL arm64 run on the device, unattended: wipe NSL, reinstall everything, verify in Game Mode.

  reset   remove NSL's launcher shortcuts, prefix and files, then refresh the Decky plugin from GitHub
  scan    add shortcuts for what's installed, the way the plugin's frontend does
  all     reset, install every launcher (nsl-arm64-autotest.sh), scan, then run nsl_arm64_gamemode_test.py

Run in Game Mode with Steam unlocked. Leaves Steam games, other tools' shortcuts and distro packages alone.
"""

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nsl_arm64_gamemode_test as gm  # noqa: E402

HOME = os.path.expanduser("~")
PLUGIN_DIR = f"{HOME}/homebrew/plugins/NonSteamLaunchers"
PLUGIN_ZIP = "https://github.com/indiesaudi/NonSteamLaunchersDecky/archive/refs/heads/arm64.zip"
HERE = os.path.dirname(os.path.abspath(__file__))
# Shortcuts NSL creates besides the launchers themselves
NSL_EXTRA_SHORTCUTS = ["NonSteamLaunchers", "Repair EA App"]
NSL_PATHS = [
    gm.PREFIX,
    f"{HOME}/.config/systemd/user/env_vars",
    f"{HOME}/.config/NonSteamLaunchers",
    f"{HOME}/Downloads/NonSteamLaunchersInstallation",
    f"{HOME}/Downloads/NonSteamLaunchers-install.log",
]

# What the plugin's frontend (src/hooks/createShortcut.tsx) does with each scan result, minus artwork
SCAN_JS = r"""
new Promise((resolve) => {
  const done = [];
  const ws = new WebSocket("ws://localhost:8675/scan");
  ws.onopen = () => ws.send("NSL e2e");
  ws.onmessage = async (e) => {
    let g;
    try { g = JSON.parse(e.data); } catch { return; }
    if (g.status === "Manual scan completed") { ws.close(); resolve(done); return; }
    if (!g.appname) return;
    if (g.Update) {
      SteamClient.Apps.SetShortcutExe(g.appid, g.exe);
      SteamClient.Apps.SetAppLaunchOptions(g.appid, g.LaunchOptions);
      done.push(g.appname + " (updated)");
      return;
    }
    const id = await SteamClient.Apps.AddShortcut(g.appname, g.exe, g.StartDir, g.LaunchOptions);
    if (!id) { done.push(g.appname + " (AddShortcut failed)"); return; }
    SteamClient.Apps.SetShortcutName(id, g.appname);
    SteamClient.Apps.SetAppLaunchOptions(id, g.LaunchOptions);
    SteamClient.Apps.SetShortcutExe(id, g.exe);
    SteamClient.Apps.SetShortcutStartDir(id, g.StartDir);
    if (g.CompatTool) SteamClient.Apps.SpecifyCompatTool(id, g.CompatTool);
    done.push(g.appname);
  };
  ws.onclose = () => resolve(done);
  setTimeout(() => { try { ws.close(); } catch {} resolve(done); }, 900000);
})
"""


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def stop_prefix():
    for sig in (signal.SIGTERM, signal.SIGKILL):
        gm.kill(gm.prefix_pids("-", chain=False), sig)
        time.sleep(3)


def reset():
    cdp = gm.Cdp()
    shortcuts = gm.read_shortcuts()
    for name in gm.LAUNCHERS + NSL_EXTRA_SHORTCUTS:
        shortcut = shortcuts.get(name)
        if not shortcut:
            continue
        gid = gm.game_id(shortcut["appid"])
        if gm.steam_running(cdp, str(shortcut["appid"])):
            gm.stop(cdp, gid)
        cdp.eval(f"SteamClient.Apps.RemoveShortcut({shortcut['appid']})")
        # Steam's own per-shortcut compat folder, if it made one
        shutil.rmtree(f"{gm.STEAM}/steamapps/compatdata/{shortcut['appid']}", ignore_errors=True)
        log(f"removed shortcut {name}")
    stop_prefix()
    for path in NSL_PATHS:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
        log(f"removed {path}")

    # Reinstall the plugin from the arm64 branch (its installer needs sudo; the plugin folder doesn't)
    with urllib.request.urlopen(PLUGIN_ZIP) as r:
        archive = zipfile.ZipFile(io.BytesIO(r.read()))
    root = archive.namelist()[0]
    for entry in os.listdir(PLUGIN_DIR):
        target = os.path.join(PLUGIN_DIR, entry)
        shutil.rmtree(target) if os.path.isdir(target) else os.remove(target)
    for member in archive.infolist():
        rel = member.filename[len(root) :]
        if not rel:
            continue
        dest = os.path.join(PLUGIN_DIR, rel)
        if member.is_dir():
            os.makedirs(dest, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(archive.read(member))
            if rel.endswith(".sh"):
                os.chmod(dest, 0o755)
    log(f"plugin refreshed from {PLUGIN_ZIP}")
    cdp.eval('DeckyBackend.call("loader/reload_plugin", "NonSteamLaunchers")')
    time.sleep(20)
    remaining = [n for n in gm.LAUNCHERS + NSL_EXTRA_SHORTCUTS if n in gm.read_shortcuts()]
    log(f"reset done; NSL shortcuts left: {remaining or 'none'}")


def scan():
    cdp = gm.Cdp()
    cdp.sock.settimeout(1000)
    added = cdp.eval(SCAN_JS) or []
    log(f"scan added/updated {len(added)}: {', '.join(added)}")
    time.sleep(10)
    return added


def main():
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    if step in ("reset", "all"):
        reset()
    if step == "all":
        log("installing every launcher")
        subprocess.run(["bash", f"{HERE}/nsl-arm64-autotest.sh"], check=False)
    if step in ("scan", "all"):
        scan()
    if step == "all":
        log("Game Mode test")
        subprocess.run([sys.executable, f"{HERE}/nsl_arm64_gamemode_test.py"], check=False)


if __name__ == "__main__":
    main()

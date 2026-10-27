#!/bin/bash
# Unattended install + launch test for the arm64 NSL launchers.
#
# For each launcher: install it with the Decky plugin's arm64 script (skipped when
# already installed), check the exe exists and record its PE arch, then launch it the
# way its Steam shortcut would (arm64 Steam Linux Runtime + aarch64 GE-Proton) and
# check it is still running after LAUNCH_WAIT seconds. Finishes with a dry-run of the
# plugin's /scan to show the shortcut each launcher would get.
#
# Usage: nsl-arm64-autotest.sh [launcher name ...]   (default: all)
# Env:   INSTALL_TIMEOUT (s, default 900), LAUNCH_WAIT (s, default 60),
#        NSL_SCRIPT (default: the installed Decky plugin's arm64 script)

set -u

# Match the Decky plugin's environment; without a display, any installer window (even an
# error box) has no driver and the installer waits forever
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-${HOME}/.Xauthority}"

STEAM="${HOME}/.local/share/Steam"
PREFIX="${STEAM}/steamapps/compatdata/NonSteamLaunchers"
DRIVE_C="${PREFIX}/pfx/drive_c"
SLR="${STEAM}/steamapps/common/SteamLinuxRuntime_4-arm64/run"
NSL_SCRIPT="${NSL_SCRIPT:-${HOME}/homebrew/plugins/NonSteamLaunchers/NonSteamLaunchers-arm64.sh}"
INSTALL_TIMEOUT="${INSTALL_TIMEOUT:-900}"
LAUNCH_WAIT="${LAUNCH_WAIT:-60}"
OUT="${HOME}/Downloads/nsl-arm64-autotest/$(date +%Y%m%d-%H%M%S)"

# Display name (as the script's option) | exe path under drive_c
LAUNCHERS=(
	"Epic Games|Program Files/Epic Games/Launcher/Portal/Binaries/Win64/EpicGamesLauncher.exe"
	"GOG Galaxy|Program Files (x86)/GOG Galaxy/GalaxyClient.exe"
	"Ubisoft Connect|Program Files (x86)/Ubisoft/Ubisoft Game Launcher/upc.exe"
	"Battle.net|Program Files (x86)/Battle.net/Battle.net Launcher.exe"
	"Amazon Games|users/steamuser/AppData/Local/Amazon Games/App/Amazon Games.exe"
	"EA App|Program Files/Electronic Arts/EA Desktop/EA Desktop/EALauncher.exe"
	"itch.io|users/steamuser/AppData/Local/itch/itch.exe"
	"Legacy Games|Program Files/Legacy Games/Legacy Games Launcher/Legacy Games Launcher.exe"
	"Humble Games Collection|Program Files/Humble App/Humble App.exe"
	"IndieGala|Program Files/IGClient/IGClient.exe"
	"Rockstar Games Launcher|Program Files/Rockstar Games/Launcher/Launcher.exe"
	"Glyph Launcher|Program Files (x86)/Glyph/GlyphClient.exe"
	"Minecraft Launcher|Program Files (x86)/Minecraft Launcher/MinecraftLauncher.exe"
	"Playstation Plus|Program Files (x86)/PlayStationPlus/pspluslauncher.exe"
	"VK Play|users/steamuser/AppData/Local/GameCenter/GameCenter.exe"
	"HoYoPlay|Program Files/HoYoPlay/launcher.exe"
	"Nexon Launcher|Program Files (x86)/Nexon/Nexon Launcher/nexon_launcher.exe"
	"Game Jolt Client|users/steamuser/AppData/Local/GameJoltClient/GameJoltClient.exe"
	"Artix Game Launcher|Program Files/Artix Game Launcher/Artix Game Launcher.exe"
	"ARC Launcher|Program Files (x86)/Arc/Arc.exe"
	"Pokémon Trading Card Game Live|users/steamuser/The Pokémon Company International/Pokémon Trading Card Game Live/Pokemon TCG Live.exe"
	"Antstream Arcade|Program Files (x86)/Antstream Ltd/Antstream/AntstreamArcade.exe"
	"PURPLE Launcher|Program Files (x86)/NC/Purple/PurpleLauncher.exe"
	"Plarium Play|users/steamuser/AppData/Local/PlariumPlay/PlariumPlay.exe"
	"VFUN Launcher|VFUN/VLauncher/VFUNLauncher.exe"
	"Tempo Launcher|Program Files/Tempo Launcher - Beta/Tempo Launcher - Beta.exe"
	"STOVE Client|ProgramData/Smilegate/STOVE/STOVE.exe"
	"Big Fish Games Manager|Program Files (x86)/bfgclient/bfgclient.exe"
	"Gryphlink|Program Files/GRYPHLINK/Launcher.exe"
)

mkdir -p "$OUT"
REPORT="${OUT}/report.md"
SUMMARY="${OUT}/summary.tsv"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "${OUT}/autotest.log"; }

proton_dir() {
	find -L "${STEAM}/compatibilitytools.d" -maxdepth 1 -type d -name "GE-Proton*-aarch64" | sort -V | tail -n1
}

# Resolve the installed exe: the expected path, else the same file name anywhere in drive_c
find_exe() {
	local rel="$1"
	if [[ -f "${DRIVE_C}/${rel}" ]]; then
		echo "${DRIVE_C}/${rel}"
		return
	fi
	find "$DRIVE_C" -type f -iname "$(basename "$rel")" -not -path "*/windows/*" 2>/dev/null | head -n1
}

pe_arch() {
	file -b "$1" 2>/dev/null | grep -oE "ARM64|Aarch64|x86-64|Intel i386|Intel 80386" | head -n1
}

# Windows processes show their exe path as the command line, so match on that
win_pids() {
	pgrep -f "^[A-Za-z]:[/\\\\].*$(basename "$1")" 2>/dev/null
}

stop_prefix() {
	local pdir="$1"
	pkill -TERM -f "^[A-Za-z]:[/\\\\]" 2>/dev/null
	sleep 5
	WINEPREFIX="${PREFIX}/pfx" "${pdir}/files/bin/wineserver" -k 2>/dev/null
	sleep 3
	pkill -KILL -f "^[A-Za-z]:[/\\\\]" 2>/dev/null
}

test_launcher() {
	local name="$1" rel="$2" slug exe arch install_result launch_result pdir
	slug=$(echo "$name" | tr -c 'A-Za-z0-9' '_')
	pdir=$(proton_dir)

	exe=$(find_exe "$rel")
	if [[ -n "$exe" ]]; then
		install_result="already-installed"
		log "${name}: already installed, skipping install"
	else
		log "${name}: installing (timeout ${INSTALL_TIMEOUT}s)"
		timeout --kill-after=30 "$INSTALL_TIMEOUT" "$NSL_SCRIPT" " ${name}" "DeckyPlugin" > "${OUT}/${slug}.install.log" 2>&1
		local rc=$?
		cp -f "${HOME}/Downloads/NonSteamLaunchers-install.log" "${OUT}/${slug}.trace.log" 2>/dev/null
		[[ $rc -eq 124 || $rc -eq 137 ]] && log "${name}: install timed out"
		stop_prefix "$pdir"
		exe=$(find_exe "$rel")
		if [[ -n "$exe" ]]; then
			install_result="installed"
		elif [[ $rc -eq 124 || $rc -eq 137 ]]; then
			install_result="FAIL(timeout)"
		else
			install_result="FAIL(no exe, rc=${rc})"
		fi
	fi

	arch="-"
	launch_result="skipped"
	if [[ -n "$exe" ]]; then
		arch=$(pe_arch "$exe")
		log "${name}: launching ${exe#"${DRIVE_C}"/} (${arch}) for ${LAUNCH_WAIT}s"
		# Steam passes the shortcut's app id; protonfixes crashes without one
		local appid
		appid=$(printf '%s' "$name" | cksum | cut -d' ' -f1)
		(
			cd "$(dirname "$exe")" || exit 1
			STEAM_COMPAT_CLIENT_INSTALL_PATH="$STEAM" STEAM_COMPAT_DATA_PATH="${PREFIX}/" \
				SteamAppId="$appid" SteamGameId="$appid" \
				"$SLR" -- "${pdir}/proton" waitforexitandrun "$exe"
		) > "${OUT}/${slug}.launch.log" 2>&1 &
		local launcher_pid=$!
		sleep "$LAUNCH_WAIT"
		if [[ -n "$(win_pids "$exe")" ]]; then
			launch_result="PASS(running)"
		elif kill -0 "$launcher_pid" 2>/dev/null; then
			launch_result="PASS(proton alive)"
		else
			launch_result="FAIL(exited)"
		fi
		stop_prefix "$pdir"
		kill "$launcher_pid" 2>/dev/null
		wait "$launcher_pid" 2>/dev/null
	fi

	log "${name}: install=${install_result} launch=${launch_result} arch=${arch}"
	printf '%s\t%s\t%s\t%s\n' "$name" "$install_result" "$launch_result" "$arch" >> "$SUMMARY"
}

# Dry-run the plugin's /scan websocket: prints what it would add, adds nothing to Steam
scan_preview() {
	python3 - <<'EOF'
import base64, json, os, socket, struct
try:
    s = socket.create_connection(("127.0.0.1", 8675), timeout=600)
except OSError as e:
    print(f"plugin backend not reachable: {e}")
    raise SystemExit
key = base64.b64encode(os.urandom(16)).decode()
s.sendall(f"GET /scan HTTP/1.1\r\nHost: localhost:8675\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
          f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
buf = b""
while b"\r\n\r\n" not in buf:
    buf += s.recv(4096)
buf = buf.split(b"\r\n\r\n", 1)[1]

def need(n):
    global buf
    while len(buf) < n:
        chunk = s.recv(65536)
        if not chunk:
            raise EOFError
        buf += chunk
    out, buf = buf[:n], buf[n:]
    return out

try:
    while True:
        h = need(2)
        op, ln = h[0] & 15, h[1] & 127
        if ln == 126:
            ln = struct.unpack(">H", need(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", need(8))[0]
        data = need(ln)
        if op == 8:
            break
        if op == 1:
            game = json.loads(data)
            if game.get("appname"):
                print(f"| {game['appname']} | {game.get('CompatTool')} | `{game.get('LaunchOptions')}` |")
except EOFError:
    pass
EOF
}

main() {
	local selected=("$@") entry name
	[[ -x "$NSL_SCRIPT" ]] || { echo "NSL script not found or not executable: $NSL_SCRIPT"; exit 1; }
	[[ -x "$SLR" ]] || { echo "arm64 Steam Linux Runtime not found: $SLR"; exit 1; }
	[[ -n "$(proton_dir)" ]] || { echo "No aarch64 GE-Proton installed"; exit 1; }

	log "Results in ${OUT}"
	for entry in "${LAUNCHERS[@]}"; do
		name="${entry%%|*}"
		if [[ ${#selected[@]} -gt 0 ]] && ! printf '%s\n' "${selected[@]}" | grep -qxF "$name"; then
			continue
		fi
		test_launcher "$name" "${entry#*|}"
	done

	{
		echo "# NSL arm64 autotest $(date '+%Y-%m-%d %H:%M')"
		echo
		echo "Proton: $(basename "$(proton_dir)") · launch wait ${LAUNCH_WAIT}s · install timeout ${INSTALL_TIMEOUT}s"
		echo
		echo "| Launcher | Install | Launch | Exe arch |"
		echo "|---|---|---|---|"
		while IFS=$'\t' read -r n i l a; do
			echo "| $n | $i | $l | $a |"
		done < "$SUMMARY"
		echo
		echo "## Shortcuts the plugin scan would add"
		echo
		echo "| Name | Compat tool | Launch options |"
		echo "|---|---|---|"
		scan_preview
	} > "$REPORT"
	log "Report: ${REPORT}"
	touch "${OUT}/DONE"
}

main "$@"

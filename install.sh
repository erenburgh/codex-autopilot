#!/bin/sh
set -eu

version="0.10.0-beta"
profile="adaptive"
install_deps=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile) profile="${2:?missing profile}"; shift 2 ;;
    --install-deps) install_deps=1; shift ;;
    -h|--help) echo "Usage: ./install.sh [--profile adaptive|host-settings] [--install-deps]"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
case "$profile" in adaptive|host-settings) ;; *) echo "Profile must be adaptive or host-settings" >&2; exit 2 ;; esac
[ "$(uname -s)" = "Darwin" ] || { echo "Codex Autopilot v0.8 public beta supports macOS only." >&2; exit 1; }

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_root=${CODEX_AUTOPILOT_INSTALL_ROOT:-"$HOME/Library/Application Support/CodexAutopilot"}
codex_bin=${CODEX_AUTOPILOT_CODEX_BIN:-"$(command -v codex || true)"}

find_python() {
  for candidate in "${CODEX_AUTOPILOT_PYTHON:-}" python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

python_bin=$(find_python || true)
if [ -z "$python_bin" ] && [ "$install_deps" -eq 1 ]; then
  command -v brew >/dev/null 2>&1 || { echo "Python 3.11+ is missing. Install Homebrew, then rerun this command." >&2; exit 1; }
  brew install python@3.13
  python_bin=$(find_python || true)
fi
[ -n "$python_bin" ] || { echo "Python 3.11+ is required internally. Rerun with --install-deps or install Python 3.11+." >&2; exit 1; }

if [ -z "$codex_bin" ] && [ "$install_deps" -eq 1 ]; then
  if ! command -v npm >/dev/null 2>&1; then
    command -v brew >/dev/null 2>&1 || { echo "Codex CLI is missing. Install it, then rerun this installer." >&2; exit 1; }
    brew install node
  fi
  npm install -g @openai/codex
  codex_bin=$(command -v codex || true)
fi
[ -n "$codex_bin" ] || { echo "Codex CLI/App Server is required. Rerun with --install-deps or install the official Codex CLI." >&2; exit 1; }
"$codex_bin" app-server --help >/dev/null 2>&1 || { echo "The installed Codex CLI does not provide App Server." >&2; exit 1; }
"$codex_bin" login status >/dev/null 2>&1 || { echo "Codex is not signed in. Run: codex login" >&2; exit 1; }

target="$install_root/$version"
mkdir -p "$install_root"
# R28: an installation the on-call engineer has already repaired is not
# destroyed. The repair gateway writes its accepted patches into
# runtime/patches and the repaired sources into runtime/src - both inside
# this very directory - and the archive loop at the end of this script skips
# the current version by name. Measured: reinstalling the same version took
# the repairs with it and left legacy-backups empty, so the self-repair the
# product promises did not survive an ordinary reinstall.
#
# The tree is renamed aside, not zipped: a rename is atomic, needs no tool,
# and the snapshot is the previous directory itself - the same convention
# the runtime uses for project state.
if [ -d "$target/runtime/patches" ] && [ -n "$(ls -A "$target/runtime/patches" 2>/dev/null)" ]; then
  repaired="$install_root/$version.repaired-$(date -u +%Y%m%dT%H%M%SZ)"
  if mv "$target" "$repaired"; then
    echo "This installation carried accepted runtime repairs; it was moved aside: $repaired"
    echo "  the patch catalogue is at $repaired/runtime/patches; nothing was deleted."
  else
    echo "Could not move the repaired installation aside from $target; nothing was removed." >&2
    exit 1
  fi
fi
rm -rf "$target"
mkdir -p "$target/runtime" "$target/bin"
# The runtime is installed as a tree of the same shape as the repository:
# not only src but everything the test suite proves behaviour with - tests,
# plugins, documentation, the installer, pyproject. The on-call engineer
# proves a repair by running that suite on a copy of the installed tree;
# from src alone it did not even assemble: 94 failures on the spot.
for item in src tests scripts build_backend plugins .agents .gitignore docs install.sh pyproject.toml README.md GETTING_STARTED.md CHANGELOG.md LICENSE; do
  [ -e "$source_dir/$item" ] && cp -R "$source_dir/$item" "$target/runtime/$item"
done
cp -R "$source_dir/plugins" "$target/plugins"
cp -R "$source_dir/.agents" "$target/.agents"
cp "$source_dir/README.md" "$source_dir/GETTING_STARTED.md" "$source_dir/LICENSE" "$target/"
"$python_bin" -m venv --without-pip "$target/venv"

cat > "$target/bin/codex-autopilot" <<'EOF'
#!/bin/sh
set -eu
base=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$base/runtime/src"
install_root=$(CDPATH= cd -- "$base/.." && pwd)
export CODEX_AUTOPILOT_INSTALL_ROOT="$install_root"
# Keep the externally visible launcher stable across version refreshes. Hook
# trust and per-tool approval are bound to their configured command, so exposing
# the resolved version directory would needlessly invalidate them on upgrade.
export CODEX_AUTOPILOT_RUNTIME="$install_root/current/bin/codex-autopilot"
exec "$base/venv/bin/python" -m codex_autopilot.cli "$@"
EOF
chmod 755 "$target/bin/codex-autopilot"

# The wake-up agent. The sleeping process that raises a retry when its time
# comes does not survive a reboot; the launchd agent sweeps the known
# projects every five minutes and arms the alarm where one is needed.
# Without it a run asleep on a rate limit would wait for a human's word
# until the next launch.
if [ "$(uname -s)" = "Darwin" ]; then
  agents_dir="$HOME/Library/LaunchAgents"
  mkdir -p "$agents_dir"
  wake_plist="$agents_dir/com.codex-autopilot.wake.plist"
  cat > "$wake_plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.codex-autopilot.wake</string>
  <key>ProgramArguments</key>
  <array>
    <string>$install_root/current/bin/codex-autopilot</string>
    <string>_wake-sweep</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>$install_root/wake-sweep.log</string>
  <key>StandardErrorPath</key><string>$install_root/wake-sweep.log</string>
</dict>
</plist>
EOF
  # The installer tests substitute HOME: loading the agent into the real
  # launchd from there is not allowed, and the variable forbids it.
  if [ -z "${CODEX_AUTOPILOT_SKIP_LAUNCHD:-}" ] && command -v launchctl >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)" "$wake_plist" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$wake_plist" >/dev/null 2>&1 || true
  fi
fi
"$python_bin" - "$target" <<'PY'
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

target = Path(sys.argv[1]).resolve()
# The `current` symlink is updated atomically below. Persist this stable path in
# plugin definitions instead of the versioned target so unchanged hooks retain
# the same command and hash after an Autopilot update.
runtime = str(target.parent / "current" / "bin" / "codex-autopilot")
cachebuster = datetime.now(timezone.utc).strftime("%Y%m%d.%H%M%S")
for path in target.glob("plugins/*/.mcp.json"):
    payload = json.loads(path.read_text(encoding="utf-8"))
    server = payload["mcpServers"]["codex_autopilot_memory"]
    if server.get("command") != "__CODEX_AUTOPILOT_RUNTIME__":
        raise SystemExit(f"unexpected MCP launcher placeholder in {path}")
    server["command"] = runtime
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
for path in target.glob("plugins/*/hooks/hooks.json"):
    payload = json.loads(path.read_text(encoding="utf-8"))
    replacements = 0
    for groups in (payload.get("hooks") or {}).values():
        for group in groups:
            for hook in group.get("hooks") or []:
                if hook.get("type") != "command":
                    continue
                expected = '"__CODEX_AUTOPILOT_RUNTIME__" hook'
                if hook.get("command") != expected:
                    raise SystemExit(f"unexpected hook launcher placeholder in {path}")
                hook["command"] = f'"{runtime}" hook'
                replacements += 1
    if replacements == 0:
        raise SystemExit(f"no command hook launcher placeholders in {path}")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
for path in target.glob("plugins/*/.codex-plugin/plugin.json"):
    payload = json.loads(path.read_text(encoding="utf-8"))
    base_version = str(payload["version"]).split("+", 1)[0]
    # Use an ordered SemVer prerelease, not build metadata. Codex compares the
    # marketplace version before refreshing its cache; build metadata alone is
    # intentionally ignored by SemVer precedence and left a stale .mcp.json.
    payload["version"] = f"{base_version}.local.{cachebuster}"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
find "$target/plugins" -type f \( -name codex-autopilot-hook -o -path '*/scripts/codex-autopilot' \) -exec chmod 755 {} \;
rm -f "$install_root/current"
ln -s "$target" "$install_root/current"

legacy_root="$HOME/.codex/skills"
backup_root="$install_root/legacy-backups"
for legacy in astra-autopilot-adaptive astra-autopilot-inherit; do
  if [ -d "$legacy_root/$legacy" ]; then
    mkdir -p "$backup_root"
    destination="$backup_root/$legacy"
    [ -e "$destination" ] && destination="$backup_root/$legacy-$(date +%Y%m%d%H%M%S)"
    echo "Found local dev preview skill: $legacy_root/$legacy"
    mv "$legacy_root/$legacy" "$destination"
    echo "Moved it to: $destination"
  fi
done

# Checking only that the marketplace exists is not enough. Another
# version's installer registers it under the resolved path of its own
# directory, and then "is it there?" answers "yes" while pointing at a
# foreign version: current is switched to this one, but the skill and the
# hooks load from the old one. Measured: after installing 0.7 over 0.8 the
# marketplace stayed at 0.7.0-beta with the 0.8 runtime beneath it. So the
# root is compared, and re-registration happens only when it is foreign -
# otherwise an ordinary update would reset hook trust for nothing.
marketplace_root=$("$codex_bin" plugin marketplace list --json 2>/dev/null | "$python_bin" -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
for item in payload.get("marketplaces", []):
    if item.get("name") == "codex-autopilot-local":
        print(item.get("root") or "")
        break
' || true)
marketplace_root=${marketplace_root%/}
if [ -z "$marketplace_root" ]; then
  "$codex_bin" plugin marketplace add "$install_root/current" >/dev/null
elif [ "$marketplace_root" != "$target" ] && [ "$marketplace_root" != "$install_root/current" ]; then
  echo "Marketplace codex-autopilot-local points at $marketplace_root; re-registering it for $version."
  "$codex_bin" plugin remove "codex-autopilot-adaptive@codex-autopilot-local" >/dev/null 2>&1 || true
  "$codex_bin" plugin remove "codex-autopilot-host-settings@codex-autopilot-local" >/dev/null 2>&1 || true
  "$codex_bin" plugin marketplace remove codex-autopilot-local >/dev/null 2>&1 || true
  "$codex_bin" plugin marketplace add "$install_root/current" >/dev/null
fi

# Codex reads the plugin NOT from the install directory but from its own
# cache. While even one old copy remained in the cache it kept loading
# that: the user had 0.9.7 on disk while 0.9.0 was running - with the old
# 30-second Interrupt declaration. Codex clamps it to 3, rewrites the
# file, the hash changes, and Stop-hook trust is lost. On every load. All
# day it looked like "the hooks keep dropping by themselves".
#
# So the profile cache is cleared entirely, the plugin is reinstalled, and
# the result is verified. One copy, its version known - or the install
# fails honestly instead of leaving the discrepancy for later.
codex_home=${CODEX_HOME:-"$HOME/.codex"}
plugin_cache="$codex_home/plugins/cache/codex-autopilot-local"
expected_version=$("$python_bin" -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' \
  "$target/plugins/codex-autopilot-$profile/.codex-plugin/plugin.json")

"$codex_bin" plugin remove "codex-autopilot-host-settings@codex-autopilot-local" >/dev/null 2>&1 || true
"$codex_bin" plugin remove "codex-autopilot-adaptive@codex-autopilot-local" >/dev/null 2>&1 || true
rm -rf "$plugin_cache/codex-autopilot-adaptive" "$plugin_cache/codex-autopilot-host-settings"
"$codex_bin" plugin add "codex-autopilot-$profile@codex-autopilot-local" >/dev/null

# Nothing but profile directories belongs in the Codex cache tree. Any
# stray folder there is a ready source of a foreign copy: Codex rescans
# the tree and restores the plugin from it. That is exactly how the 0.9.0
# copy came back after being set "aside" inside the same cache.
if [ -d "$plugin_cache" ]; then
  for stray in "$plugin_cache"/* "$plugin_cache"/.[!.]*; do
    [ -e "$stray" ] || continue
    case "$(basename "$stray")" in
      codex-autopilot-adaptive|codex-autopilot-host-settings) ;;
      *) echo "Removing a stray item from the Codex cache: $(basename "$stray")"; rm -rf "$stray" ;;
    esac
  done
fi

cached_dirs=$(ls -d "$plugin_cache/codex-autopilot-$profile"/*/ 2>/dev/null | wc -l | tr -d ' ')
cached_version=$("$python_bin" - "$plugin_cache/codex-autopilot-$profile" <<'PYCHECK'
import json, sys
from pathlib import Path
roots = sorted(Path(sys.argv[1]).glob("*/.codex-plugin/plugin.json"))
print(json.loads(roots[0].read_text(encoding="utf-8"))["version"] if len(roots) == 1 else "")
PYCHECK
)
if [ "$cached_dirs" = "0" ]; then
  # Codex does not pick the copy up instantly. That is not a discrepancy -
  # preflight checks again before the run and will not admit a foreign copy.
  echo "The plugin has not appeared in the Codex cache yet; preflight will verify it before the run."
elif [ "$cached_dirs" != "1" ] || [ "$cached_version" != "$expected_version" ]; then
  echo "The plugin in the Codex cache does not match the installed one." >&2
  echo "  installed: $expected_version" >&2
  echo "  in cache:  ${cached_version:-<copies: $cached_dirs>}" >&2
  echo "Launching in this state would load a foreign copy: stopped." >&2
  exit 1
fi

# Autopilot's own script is registered in the Codex execpolicy; otherwise
# launching it may hit a native dialog the dispatcher never answers.
# Details and measurements are in scripts/register_execpolicy.py.
installed_script=$(ls -d "$codex_home/plugins/cache/codex-autopilot-local/codex-autopilot-$profile"/*/skills/"codex-autopilot-$profile"/scripts/codex-autopilot 2>/dev/null | tail -1)
if [ -n "$installed_script" ]; then
  "$python_bin" "$source_dir/scripts/register_execpolicy.py" --script "$installed_script" --rules "$codex_home/rules/default.rules"
else
  echo "Execpolicy: installed script not found in the plugin cache; skipped. Codex will ask for approval on each start."
fi

# Previous installations no longer stay lying next door. While there were
# thirteen of them, any one could become the source of a foreign copy, and
# the gap between "installed" and "running" cost the user a whole night.
# They are not deleted but packed into one archive alongside.
legacy_zip="$install_root/legacy-backups/previous-installs-$(date +%Y%m%d-%H%M%S).zip"
mkdir -p "$install_root/legacy-backups"
pruned=0
for previous in "$install_root"/*/; do
  name=$(basename "$previous")
  case "$name" in
    "$version"|current|legacy-backups) continue ;;
    # A tree moved aside because it carried accepted runtime repairs. It is
    # not an old version to be packed away: it belongs to the version being
    # installed right now, and the line above printed its path to the user.
    # Zipping it here would delete the directory that message names.
    *.repaired-*) continue ;;
  esac
  case "$name" in
    [0-9]*) ;;
    *) continue ;;
  esac
  if (cd "$install_root" && zip -rq "$legacy_zip" "$name") then
    rm -rf "$previous"
    pruned=$((pruned + 1))
  fi
done
if [ "$pruned" -gt 0 ]; then
  echo "Previous installations moved to an archive: $pruned -> $legacy_zip"
fi

echo "Codex Autopilot $version installed with the $profile profile."
echo "Codex safety requires one trust review after install or a real hook-definition change: open /hooks in Codex and trust the current Codex Autopilot hooks."
echo "Start a fresh Codex task, then say: Use Codex Autopilot for this project."
echo "On first use, a dedicated preflight task probes the single local memory tool before Worker 1. Choose Always only if you trust this installed plugin; Autopilot never answers for you."
echo "The target Git project may be different from the initiating task directory."
echo "The first run performs a deterministic preflight and names any exact permission it needs before creating run-state."

#!/bin/sh
set -eu

version="0.7.0-beta"
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
[ "$(uname -s)" = "Darwin" ] || { echo "Codex Autopilot v0.7 public beta supports macOS only." >&2; exit 1; }

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
rm -rf "$target"
mkdir -p "$target/runtime" "$target/bin"
cp -R "$source_dir/src" "$target/runtime/src"
cp -R "$source_dir/plugins" "$target/plugins"
cp -R "$source_dir/.agents" "$target/.agents"
cp "$source_dir/README.md" "$source_dir/GETTING_STARTED.md" "$source_dir/LICENSE" "$target/"
"$python_bin" -m venv --without-pip "$target/venv"

cat > "$target/bin/codex-autopilot" <<'EOF'
#!/bin/sh
set -eu
base=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$base/runtime/src"
export CODEX_AUTOPILOT_INSTALL_ROOT=$(CDPATH= cd -- "$base/.." && pwd)
exec "$base/venv/bin/python" -m codex_autopilot.cli "$@"
EOF
chmod 755 "$target/bin/codex-autopilot"
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

"$codex_bin" plugin remove "codex-autopilot-adaptive@codex-autopilot-local" >/dev/null 2>&1 || true
"$codex_bin" plugin remove "codex-autopilot-host-settings@codex-autopilot-local" >/dev/null 2>&1 || true
"$codex_bin" plugin marketplace remove codex-autopilot-local >/dev/null 2>&1 || true
"$codex_bin" plugin marketplace add "$install_root/current" >/dev/null
"$codex_bin" plugin add "codex-autopilot-$profile@codex-autopilot-local" >/dev/null

echo "Codex Autopilot $version installed with the $profile profile."
echo "Codex safety requires one trust review for the plugin hooks: open /hooks in Codex and trust Codex Autopilot."
echo "Then open a Git project and say: Use Codex Autopilot for this project."

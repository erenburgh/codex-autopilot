#!/bin/sh
set -eu

version="0.9.9-beta"
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
install_root=$(CDPATH= cd -- "$base/.." && pwd)
export CODEX_AUTOPILOT_INSTALL_ROOT="$install_root"
# Keep the externally visible launcher stable across version refreshes. Hook
# trust and per-tool approval are bound to their configured command, so exposing
# the resolved version directory would needlessly invalidate them on upgrade.
export CODEX_AUTOPILOT_RUNTIME="$install_root/current/bin/codex-autopilot"
exec "$base/venv/bin/python" -m codex_autopilot.cli "$@"
EOF
chmod 755 "$target/bin/codex-autopilot"
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

# Проверять только наличие marketplace недостаточно. Установщик другой
# версии регистрирует его по разрешённому пути своего каталога, и тогда
# "есть?" отвечает "есть", указывая на чужую версию: current перевешен на
# эту, а скилл и хуки грузятся из прежней. Замерено: после установки 0.7
# поверх 0.8 marketplace остался на 0.7.0-beta, рантайм под ним - 0.8.
# Поэтому сверяется корень, и перерегистрация делается только когда он
# чужой - иначе обычное обновление зря сбрасывало бы доверие хукам.
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

# Codex читает плагин НЕ из каталога установки, а из своего кэша. Пока в
# кэше оставалась хоть одна прежняя копия, он продолжал грузить её: у
# пользователя лежал 0.9.7, а работал 0.9.0 - с прежним объявлением
# Interrupt на 30 секунд. Codex зажимает его до 3, переписывает файл,
# хэш меняется, и доверие Stop-хука слетает. Каждая загрузка. Весь день
# это выглядело как "хуки слетают сами".
#
# Поэтому кэш профиля вычищается целиком, плагин переустанавливается, а
# результат сверяется. Одна копия, её версия известна - или установка
# честно падает, а не оставляет расхождение на потом.
codex_home=${CODEX_HOME:-"$HOME/.codex"}
plugin_cache="$codex_home/plugins/cache/codex-autopilot-local"
expected_version=$("$python_bin" -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' \
  "$target/plugins/codex-autopilot-$profile/.codex-plugin/plugin.json")

"$codex_bin" plugin remove "codex-autopilot-host-settings@codex-autopilot-local" >/dev/null 2>&1 || true
"$codex_bin" plugin remove "codex-autopilot-adaptive@codex-autopilot-local" >/dev/null 2>&1 || true
rm -rf "$plugin_cache/codex-autopilot-adaptive" "$plugin_cache/codex-autopilot-host-settings"
"$codex_bin" plugin add "codex-autopilot-$profile@codex-autopilot-local" >/dev/null

# В дереве кэша Codex не должно быть ничего, кроме каталогов профилей.
# Любая посторонняя папка там - готовый источник чужой копии: Codex
# пересканирует дерево и восстановит плагин из неё. Именно так вернулась
# копия 0.9.0, отложенная "в сторонку" внутри того же кэша.
if [ -d "$plugin_cache" ]; then
  for stray in "$plugin_cache"/* "$plugin_cache"/.[!.]*; do
    [ -e "$stray" ] || continue
    case "$(basename "$stray")" in
      codex-autopilot-adaptive|codex-autopilot-host-settings) ;;
      *) echo "Убираю постороннее из кэша Codex: $(basename "$stray")"; rm -rf "$stray" ;;
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
  # Codex забирает копию не мгновенно. Это не расхождение - preflight
  # перед прогоном сверит ещё раз и не пустит чужую копию.
  echo "Плагин ещё не появился в кэше Codex; preflight сверит его перед прогоном."
elif [ "$cached_dirs" != "1" ] || [ "$cached_version" != "$expected_version" ]; then
  echo "Плагин в кэше Codex не совпадает с установленным." >&2
  echo "  установлено: $expected_version" >&2
  echo "  в кэше:      ${cached_version:-<копий: $cached_dirs>}" >&2
  echo "Запуск в таком состоянии грузил бы чужую копию: остановлено." >&2
  exit 1
fi

# Собственный скрипт Autopilot прописывается в execpolicy Codex, иначе его
# запуск может упереться в нативный диалог, на который диспетчер не отвечает.
# Подробности и замеры - в scripts/register_execpolicy.py.
installed_script=$(ls -d "$codex_home/plugins/cache/codex-autopilot-local/codex-autopilot-$profile"/*/skills/"codex-autopilot-$profile"/scripts/codex-autopilot 2>/dev/null | tail -1)
if [ -n "$installed_script" ]; then
  "$python_bin" "$source_dir/scripts/register_execpolicy.py" --script "$installed_script" --rules "$codex_home/rules/default.rules"
else
  echo "Execpolicy: installed script not found in the plugin cache; skipped. Codex will ask for approval on each start."
fi

# Прежние установки больше не остаются лежать рядом. Пока их было
# тринадцать, любая из них могла стать источником чужой копии, а разница
# между "установлено" и "работает" стоила пользователю целой ночи. Они не
# удаляются, а складываются в один архив рядом.
legacy_zip="$install_root/legacy-backups/previous-installs-$(date +%Y%m%d-%H%M%S).zip"
mkdir -p "$install_root/legacy-backups"
pruned=0
for previous in "$install_root"/*/; do
  name=$(basename "$previous")
  case "$name" in
    "$version"|current|legacy-backups) continue ;;
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
  echo "Прежних установок убрано в архив: $pruned -> $legacy_zip"
fi

echo "Codex Autopilot $version installed with the $profile profile."
echo "Codex safety requires one trust review after install or a real hook-definition change: open /hooks in Codex and trust the current Codex Autopilot hooks."
echo "Start a fresh Codex task, then say: Use Codex Autopilot for this project."
echo "On first use, a dedicated preflight task probes the single local memory tool before Worker 1. Choose Always only if you trust this installed plugin; Autopilot never answers for you."
echo "The target Git project may be different from the initiating task directory."
echo "The first run performs a deterministic preflight and names any exact permission it needs before creating run-state."

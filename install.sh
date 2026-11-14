#!/usr/bin/env bash
# agent-friction installer.
#
#   curl -fsSL https://raw.githubusercontent.com/martinwheeler/friction/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --no-services     # options go to installer.py
#   ./install.sh                                             # from a checkout: installs that checkout
#
# Re-run to update. `./install.sh --uninstall` removes everything but your data.
set -euo pipefail

REPO="${AGENT_FRICTION_REPO:-https://github.com/martinwheeler/friction.git}"
APP="${AGENT_FRICTION_APP:-$HOME/.local/share/agent-friction/app}"

die() { echo "agent-friction: $*" >&2; exit 1; }

command -v python3 >/dev/null || die "python3 is required (3.8+)"

here=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -n "$here" ] && [ -f "$here/installer.py" ]; then
  APP="$here"
else
  command -v git >/dev/null || die "git is required to download agent-friction"
  if [ -d "$APP/.git" ]; then
    echo "Updating $APP"
    git -C "$APP" pull --ff-only --quiet
  else
    echo "Downloading agent-friction to $APP"
    mkdir -p "$(dirname "$APP")"
    git clone --depth 1 --quiet "$REPO" "$APP"
  fi
fi

exec python3 "$APP/installer.py" "$@"

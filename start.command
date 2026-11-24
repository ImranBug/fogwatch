#!/bin/zsh
cd "$(dirname "$0")"
command -v uv >/dev/null || { echo "Fogwatch needs uv: curl -LsSf https://astral.sh/uv/install.sh | sh   (then open this file again)"; read; exit 1; }
[ -f data/token ] || uv run tools.py install-gsi   # first run: tell Dota where to send game data
pkill -f "live-coach/.venv/bin/python3 app.py" 2>/dev/null; sleep 2
# close earlier coach windows that have finished ("[Process completed]"); this window is still busy, so it stays
osascript -e 'tell application "Terminal"
  repeat with i in (id of every window whose name contains "start.command")
    try
      if (busy of tab 1 of window id i) is false then close window id i
    end try
  end repeat
end tell' >/dev/null 2>&1
(sleep 6; open http://localhost:3939/) &   # ponytail: always opens the dashboard; an already-open tab also reloads itself, so a restart can leave a duplicate tab
uv run app.py 2>&1 | tee data/coach.log

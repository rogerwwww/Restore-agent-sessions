#!/usr/bin/env bash
set -euo pipefail

repo_raw="https://raw.githubusercontent.com/rogerwwww/Restore-agent-sessions/main"
bin_dir="${HOME}/.local/bin"
session_dir="${HOME}/.agent_sessions"

mkdir -p "${bin_dir}" "${session_dir}"

curl -fsSL "${repo_raw}/bin/agent-live-sessions" -o "${bin_dir}/agent-live-sessions"
curl -fsSL "${repo_raw}/bin/agent-restore-sessions" -o "${bin_dir}/agent-restore-sessions"
chmod +x "${bin_dir}/agent-live-sessions" "${bin_dir}/agent-restore-sessions"

cron_line="*/5 * * * * ${bin_dir}/agent-live-sessions >/dev/null 2>&1"
if command -v crontab >/dev/null 2>&1; then
  current_cron="$(crontab -l 2>/dev/null || true)"
  if ! printf '%s\n' "${current_cron}" | grep -Fq "${bin_dir}/agent-live-sessions"; then
    {
      printf '%s\n' "${current_cron}"
      printf '%s\n' "# Refresh live Codex/Claude session recovery snapshot."
      printf '%s\n' "${cron_line}"
    } | sed '/^$/d' | crontab -
  fi
fi

"${bin_dir}/agent-live-sessions" || true

echo "Installed agent-live-sessions and agent-restore-sessions."
echo "Recovery snapshot: ${session_dir}/live_sessions.md"

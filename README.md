# Restore Agent Sessions

Restore your Codex and Claude Code sessions in WSL+Windows Terminal, so that you won't lose your progress if your PC reboots.

## Demo

![Restore agent sessions demo](agent-restore-showcase.gif)

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/rogerwwww/Restore-agent-sessions/main/install.sh | bash
```

## Use

```bash
agent-live-sessions
agent-restore-sessions
```

Session snapshots are stored in `~/.agent_sessions/`.

Internal Codex Guardian approval-review sessions are excluded from new snapshots
and skipped when restoring older snapshots, including with `--force`. Detection
uses rollout metadata (`thread_source: guardian_review` or
`source.subagent.other: guardian`), rather than conversation titles or content.
Codex approval review continues to operate normally.

Spawned Codex child-agent threads are never resumed independently, including
when restoring older snapshots with `--force`. Recovery follows their parent
metadata to the root conversation and restores each root once. If the parent
rollout is missing or invalid, the child is skipped. Ordinary user-created
forks remain independently recoverable.

Codex IDs and working directories come from the rollout header. Some rollout
filenames include an extra writer UUID, and the shared daemon's working
directory may belong to a different project. Older snapshots are corrected
when restored. Open writer locks and running `codex resume <id>` clients also
provide recovery evidence when the daemon no longer has an open rollout file.

If a daemon restart or disconnect already removed sessions from the latest
snapshot, restore a complete earlier snapshot from the append-only history:

```bash
agent-restore-sessions --at '2026-11-10T11:25:01-05:00' --tool codex --dry-run
# Remove --dry-run to open the selected sessions.
```

`--at` selects the latest recorded scan at or before the supplied timestamp;
include a timezone. It can be combined with the usual live-session checks,
`--force`, and `--limit`. Use `--history PATH` for a different history file.
History is not merged automatically, since it also contains sessions that
were intentionally closed. A disconnected client launched without an explicit
resume ID may require this history recovery if no rollout or writer lock is open.

#!/bin/sh
# launchd wrapper for the groundcrew daemon.
#
# launchd has no EnvironmentFile= and plists are effectively world-readable,
# so secrets (notifier credentials) live in ~/.config/groundcrew/env (0600)
# and this wrapper sources it. It also fixes PATH — launchd's default lacks
# everything — and converts groundcrew's config-error exit (78) into a
# successful exit, which is how a KeepAlive={SuccessfulExit=false} job says
# "stop relaunching me": a config typo halts the service until a human fixes
# it, mirroring systemd's RestartPreventExitStatus=78.

set -a
[ -f "$HOME/.config/groundcrew/env" ] && . "$HOME/.config/groundcrew/env"
set +a

# mise shims first (repo toolchains for sessions), then the native installers'
# bin, then Homebrew for git-lfs and friends.
export PATH="$HOME/.local/share/mise/shims:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

groundcrew daemon
s=$?
[ "$s" = 78 ] && exit 0
exit "$s"

# Install

```sh
uv tool install git+https://github.com/bojanrajkovic/groundcrew
```

(Developing groundcrew itself? `uv tool install --editable <checkout>`.)
Service templates referenced below live in the repo's `systemd/` and
`launchd/` directories — grab them from the repo even without a checkout.
After the service is running, see [Configuration](configuration.md) for the
config file, notifier commands, and the post-pull hook.

## Linux (systemd)

```sh
mkdir -p ~/.config/systemd/user ~/.config/groundcrew
curl -fsSL -o ~/.config/systemd/user/groundcrew.service \
  https://raw.githubusercontent.com/bojanrajkovic/groundcrew/main/systemd/groundcrew.service
systemctl --user daemon-reload
systemctl --user enable --now groundcrew
```

One-time host preparation:

- `loginctl enable-linger <user>` so the unit runs without a login session
- `mise settings set trusted_config_paths ~/Projects` so headless
  `mise install` never blocks on a trust prompt (mise users only)
- optional `~/.config/groundcrew/env` (mode 0600) for secrets the notifier
  needs and environment overrides:

```
PUSHOVER_TOKEN=...   # read by contrib/notify-pushover, not by groundcrew
PUSHOVER_USER=...
# SSH_AUTH_SOCK=/run/user/1000/... if ssh-agent auth is needed for pulls
```

### Reading the logs

groundcrew installs as a **user** unit, so its journal is in the user scope.
`journalctl -u groundcrew` searches the system scope and reports no entries:

```sh
journalctl --user -u groundcrew -f          # follow
journalctl --user -u groundcrew -p warning  # warnings and errors only
```

The daemon logs state changes — spawns, deaths, retirements, drift, pull
failures — not ticks. A healthy fleet is quiet between them; `groundcrew
status` prints how long ago the last poll wrote state, which is the liveness
check the journal deliberately does not repeat every 30 seconds.

Supervisor output is separate. Each `claude remote-control` child writes to
`~/.local/state/groundcrew/logs/<repo-path>.log`, keeping the fleet's own
records out of the daemon's journal, where the volume would trip journald's
per-unit rate limit and drop the daemon's lines with it.

## macOS (launchd)

Setup uses the two templates in `launchd/`: a LaunchAgent plist and a
wrapper script that sources `~/.config/groundcrew/env` (0600) for notifier
secrets, fixes PATH, and stops the relaunch loop on a config error — the
comments in both files explain each choice.

```sh
mkdir -p ~/.config/groundcrew ~/Library/Logs
cp launchd/groundcrew-wrapper.sh ~/.config/groundcrew/
chmod 755 ~/.config/groundcrew/groundcrew-wrapper.sh
sed "s/CHANGEME/$USER/g" launchd/com.groundcrew.daemon.plist \
  > ~/Library/LaunchAgents/com.groundcrew.daemon.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.groundcrew.daemon.plist
```

Stop/start with `launchctl kickstart -k gui/$(id -u)/com.groundcrew.daemon`
(the `-k` restart never bounces supervisors — `AbandonProcessGroup` is the
`KillMode=process` equivalent, and the next instance re-adopts them);
remove with `launchctl bootout gui/$(id -u)/com.groundcrew.daemon`.

Honest caveats:

- **No linger equivalent.** LaunchAgents run inside a login session; a
  headless Mac needs either an auto-login user or a system LaunchDaemon —
  and a LaunchDaemon runs as root outside the user context, which breaks
  Claude's per-user credentials and keychain. Practical answer: keep the
  user logged in.
- **Sleep is not a restart.** launchd does not re-run jobs on wake; the
  daemon's poll loop tolerates sleep fine, but hourly ticks slip by however
  long the lid was closed.
- **Homebrew paths.** The native `claude` installer uses `~/.local/bin` on
  macOS too, but a Homebrew-installed `claude` lands in `/opt/homebrew/bin`
  — point `[claude].bin` at it in [config.toml](configuration.md).
- **No journald.** Logs go to `~/Library/Logs/groundcrew.log`; rotate it
  yourself (`newsyslog.d`) if the fleet is chatty. Because a flat file has no
  record fields, lines there carry their own timestamp and level, where the
  journald format leaves both to the journal.

## Upgrading from a pre-config version

The registry moved out of the groundcrew checkout, and Pushover/mise
built-ins became config. Do this **before** restarting the daemon on the
new version — a daemon that finds no registry sees an empty fleet and
retires adopted supervisors once they go quiet:

```sh
mkdir -p ~/.config/groundcrew
mv <old-checkout>/repos.toml ~/.config/groundcrew/repos.toml
cat > ~/.config/groundcrew/config.toml <<'EOF'
[notify]
command = ["notify-pushover"]        # or an absolute path to the script

[hooks]
post_pull = ["mise", "install"]
EOF
systemctl --user daemon-reload && systemctl --user restart groundcrew
```

The restart is safe: supervisors survive and are re-adopted. They then show
args-drift (their command lines predate explicit flags) and converge one by
one through the quiet gate and spawn ramp — expect the fleet to roll over
the following hours without losing any session. The written config above is
the minimal migration; [Configuration](configuration.md) has the full
surface.

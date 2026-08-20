# groundcrew

Fleet supervision for `claude remote-control`: one daemon keeps a supervisor
alive in every registered repository and keeps the fleet fresh without losing
in-flight sessions.

## Language

**Supervised repo**:
The unit of supervision: one registered directory together with its
supervisor, sessions, freshness state, warnings, and crash history. The
daemon is a fleet of these plus fleet-wide concerns (updates, rollouts).
A directory git does not manage is supervised too; freshness pulls, worktree
spawns, and parking do not apply.
_Avoid_: runtime, repo state

**Supervisor**:
The long-lived `claude remote-control` process groundcrew runs in a repo. One
per registered repo.
_Avoid_: rc process, worker, instance

**Engine**:
A per-session child process the supervisor spawns to run one Claude session.
_Avoid_: session process, child

**Registry**:
The machine-written list of repos groundcrew manages (`repos.toml`).
Maintained by `groundcrew add` / `remove`; never holds settings.
_Avoid_: repo list, config

**Config**:
The human-written settings file (`config.toml`). Never rewritten by
groundcrew.

**Override**:
A per-repo settings table in the config that replaces a global setting for
one repo.

**Notifier**:
The user-configured command groundcrew invokes to deliver a daemon alert.
groundcrew has no built-in delivery providers.
_Avoid_: provider, plugin, integration

**Post-pull hook**:
The user-configured command run in a repo after its default branch moves,
to refresh toolchains or dependencies.
_Avoid_: mise step, install step

**Quiet**:
A session with no transcript writes for the quiet window; a repo is quiet
when all its sessions are. Only quiet repos are restarted.
_Avoid_: idle (sessions never report idle; quiet is inferred)

**Parked**:
A repo whose working tree sits on a branch other than its default branch.
Parked repos get ref-only fetches, never pulls.

**Drift**:
A running supervisor's binary version differing from the installed `claude`
binary. Drift triggers a restart once the repo is quiet.

**Adoption**:
A restarted daemon reattaching to supervisors it did not spawn, matched by
command line and working directory.
_Avoid_: orphan recovery, reparenting

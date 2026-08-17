# Decisions as values, effects at the edges

The supervision core is a functional core in an imperative shell. The
supervised repo (see CONTEXT.md) is an entity whose methods take world
observations as values (a `PullOutcome`, a quiet flag, a probed version) and
return decisions as values (spawn, restart, run-hook, alert); its only
mutation is its own bookkeeping. The daemon is the shell: it performs
effects, feeds outcomes back in, and executes the decisions it is handed —
including fleet policies like the spawn ramp, which throttles the execution
of spawn decisions rather than living inside per-repo logic.

There is deliberately no injected effects interface. The alternatives — one
Effects ABC, segregated per-domain interfaces — were weighed and rejected:
the sub-seams never vary independently (live world and test world swap as a
set), and interaction-style tests assert on recorded fake calls rather than
outcomes. Instead, behavior is tested as values in / values out with no
substrate and no fakes, while the plumbing (git, processes, scripts,
session files) keeps its real-substrate module tests. The cost is a
conversation protocol between shell and entity (observe, decide, execute,
feed back) that the shell must call in order.

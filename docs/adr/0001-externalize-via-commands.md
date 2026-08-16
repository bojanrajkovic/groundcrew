# Externalize integrations via commands, not built-in providers

groundcrew needs to deliver alerts and refresh toolchains, and both are
workflow-specific: one user wants Pushover and mise, the next wants ntfy and
pnpm. Instead of maintaining an in-tree provider for each, groundcrew exposes
exactly one mechanism for each concern: a user-configured command. The
notifier command receives the alert as arguments and environment variables;
the post-pull hook runs in the repo after its default branch moves. The
original built-in Pushover delivery and mise auto-detection were deliberately
removed in favor of this — a Pushover example ships in `contrib/` instead.

Consequence: groundcrew never grows provider code. A request to "support X"
is answered with a script, not a patch.

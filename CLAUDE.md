# groundcrew

Fleet daemon keeping `claude remote-control` environments alive across local
repos. Read `docs/architecture.md` before changing daemon or supervision
behavior; `docs/install.md` covers deployment and `docs/configuration.md`
the config surface.

Quality gate: `uv run ruff check src tests && uv run mypy src tests && uv run pytest`.

## Agent skills

### Issue tracker

Issues live in GitHub Issues (`bojanrajkovic/groundcrew`) via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root, created lazily. See `docs/agents/domain.md`.

## Working on groundcrew

These are steps to follow whenever we're building new9;9ufeatures, fixing bugs, etc.:

* Run the /grill-with-docs skill to ask clarifying questions, sharpen the idea, brainstorm
  alternatives, and iterate on the design.
* Read & explore the code for facts, use documentation as a guidepost.
* Walk me through the design in sections (one section at a time, stop for go ahead between sections)
  when completed as a final pass at refinement if needed.
* Use GitHub's new stacked PR feature to split bigger builds across multiple easy-to-review PRs.
* Load the Skill tool and use the `/ponytail` skill in ultra mode before designing
* Load the Skill tool and run `/ponytail-review` after designing and after writing code.
* Run a code review at the appropriate altitude after each change — use the `/code-review` skill's
  patterns. Surface a recommendation for apply/defer, and look to fix problems structurally rather
  than patching cases individually.
* Write/update documentation with the change, don't batch it for later.
* Use TDD whenever it makes sense to.
* Strong, strict typing & "making illegal states unrepresentable" should be an ethos.

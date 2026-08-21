# Registry entries carry their settings

`repos.toml` holds one `[[repos]]` entry per managed directory: a path, plus
the settings that differ from the global defaults in `config.toml`. Path
identity is established in one file, by one type whose only constructor
normalizes, so settings for an unregistered directory are unparseable rather
than warned about. The array-of-tables shape is forced rather than preferred —
a flat `repos = [...]` and `[repos."<path>"]` tables cannot coexist in one TOML
file, since the key would need two types at once.

The cost is that `repos.toml` becomes groundcrew's file. `add` and `remove`
re-emit it whole, so comments a human writes there do not survive; preserving
them needs a comment-aware TOML writer, the dependency ADR 0002 declined. The
format change is one-way: an older groundcrew reads `repos = [...]`, not
`[[repos]]`. And moving the existing settings out of `config.toml` requires
groundcrew to write that file exactly once, a deliberate exception to ADR
0002's rule, taken with `config.toml.bak` left beside it.

Two cheaper options were weighed and rejected. A shared normalization helper
across the two readers fixes paths that key differently in each file, but
leaves the two-key shape that produced them. A `[settings."<path>"]` table
beside `repos = [...]` moves the settings into the registry file and
reproduces that shape inside it, with the same two keys to keep in agreement.

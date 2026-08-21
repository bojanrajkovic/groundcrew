# Split human config from machine registry

Global settings live in `config.toml`, which groundcrew never writes; the
managed directory list lives in `repos.toml`, which `groundcrew add`/`remove`
rewrite freely. The split exists because the standard library's TOML support
is read-only — rewriting a file a human maintains would destroy comments and
formatting on every `add`. Per-repo settings travel with the registry entry
they belong to (ADR 0005).

Consequence: a directory path is identity, so it gets exactly one spelling.
One type owns that normalization and its only constructor applies it. Paths
used as executables (`claude.bin`, hook commands) stay unresolved, because
resolving a symlinked binary pins the version behind it.

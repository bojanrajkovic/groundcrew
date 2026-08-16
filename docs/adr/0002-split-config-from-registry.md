# Split human config from machine registry

Settings live in `config.toml`, which groundcrew never writes; the managed
repo list lives in `repos.toml`, which `groundcrew add`/`remove` rewrite
freely and which never holds settings. The split exists because the standard
library's TOML support is read-only — rewriting a file a human maintains
would destroy comments and formatting on every `add`. Per-repo overrides
therefore live in `config.toml` (they are settings), keyed by repo path,
while the registry stays a flat list of paths.

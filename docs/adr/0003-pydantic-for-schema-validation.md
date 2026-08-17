# Pydantic for every schema surface

groundcrew validates two schemas: the human-written config file and the
fleet state snapshot shared by the daemon and `status`. Both are defined as
pydantic models (strict, `extra="forbid"`) rather than hand-rolled
validation walkers — one idiom for every schema, with a small translation
layer keeping config errors phrased as TOML key paths
(`config.toml: [claude].capacity must be an integer`). The earlier
hand-rolled loader worked, but each new schema surface was reimplementing
type checking, unknown-key rejection, and error naming; models make illegal
states unrepresentable once, and new surfaces inherit that for free.

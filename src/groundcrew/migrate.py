"""One-time migration of the per-repo settings config.toml used to hold.

A managed directory's settings were a ``[repos."<path>"]`` table in
config.toml. They are `[[repos]]` entries in repos.toml now (ADR 0005), and the
config-file schema has no `repos` key at all, so an un-migrated file fails to
load rather than getting migrated on the way through. `main` runs this before
`load`, and that is the only trigger.

Nothing here is durable. Once every machine has run a version containing it,
delete the module and the two lines in cli.py that reach it.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from pydantic import ValidationError

from groundcrew.config import (
    ConfigError,
    _RegistryEntry,
    _translate,
    atomic_write,
    config_dir,
    load_registry,
    save_registry,
)

_CANNOT_STRIP = (
    "config.toml: the per-repo settings cannot be moved automatically — each one has to be a "
    '[repos."<path>"] header at the start of its own line. Nothing was changed; rewrite them '
    "that way, or move them into repos.toml as [[repos]] entries and delete them here."
)


def _heading_block(lines: list[str], last: int) -> int:
    """Length of the contiguous comment block ending at `last`, if it is a heading.

    Zero when a setting line touches the block from underneath: such a comment
    documents that setting and stays with it. A blank line above the block, or
    the start of the region, makes the block a heading for whatever follows.
    """
    block = 0
    while last - block >= 0 and lines[last - block].startswith("#"):
        block += 1
    if block and (last - block < 0 or not lines[last - block].strip()):
        return block
    return 0


def _strip_repo_tables(text: str) -> str:
    """config.toml without its [repos."..."] tables, checked against a real parse."""
    lines = text.splitlines(keepends=True)
    keep: list[str] = []
    i = 0
    while i < len(lines):
        if not lines[i].startswith("[repos."):
            keep.append(lines[i])
            i += 1
            continue
        # A heading block above the header documents the table; it goes too.
        if block := _heading_block(keep, len(keep) - 1):
            del keep[len(keep) - block :]
        i += 1
        while i < len(lines) and not lines[i].startswith("["):
            i += 1
        # The same rule at the other end of the table: a heading block sitting
        # directly above the NEXT header documents that section, so stop short
        # of it. At EOF nothing follows, so the block is this table's trailer.
        if i < len(lines) and (block := _heading_block(lines, i - 1)):
            i -= block
        while keep and not keep[-1].strip():
            keep.pop()
        # Removing a table from the middle of the file would otherwise weld the
        # sections around it together. Adjacent removals collapse to one blank,
        # and a table at either end of the file adds none.
        if keep and i < len(lines):
            keep.append("\n")
    cleaned = "".join(keep)

    # The scan is dumb on purpose, because this is the part that has to hold:
    # every key and value except `repos` survives byte for byte through a real
    # parse, and what is left is still TOML. Headers match at column 0 and are
    # never stripped first, or an array element like `  ["a"],` reads as one.
    before = tomllib.loads(text)
    before.pop("repos", None)
    try:
        after = tomllib.loads(cleaned)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(_CANNOT_STRIP) from exc
    if after != before:
        raise ConfigError(_CANNOT_STRIP)
    return cleaned


def _entry(raw: str, table: object) -> _RegistryEntry:
    """One legacy table as the registry entry it has to become, or why it cannot."""
    if not isinstance(table, dict):
        raise ConfigError(f'config.toml: [repos."{raw}"] must be a table of settings')
    try:
        # The header names the path; `path` is the field that normalizes it.
        return _RegistryEntry.model_validate({**table, "path": raw})
    except ValidationError as exc:
        raise _translate(exc, f'config.toml [repos."{raw}"]') from exc


def _reject_clashing_spellings(raws: list[str], entries: list[_RegistryEntry]) -> None:
    """`~/Working` and `/home/u/Working` are one directory under two TOML keys.

    Merging them would keep whichever came last and report both as migrated, so
    the settings the user loses are the ones the report says are safe.
    """
    spellings: dict[Path, list[str]] = {}
    for raw, entry in zip(raws, entries, strict=True):
        spellings.setdefault(entry.path, []).append(raw)
    clashes = [(path, raw) for path, raw in spellings.items() if len(raw) > 1]
    if clashes:
        detail = "; ".join(f"{p} is written as {' and '.join(map(repr, r))}" for p, r in clashes)
        raise ConfigError(
            f"config.toml: two per-repo tables name one directory ({detail}). "
            "Nothing was changed; merge them into a single table."
        )


def _settings(entry: _RegistryEntry) -> dict[str, object]:
    """What the entry states, in field order. The path is its key, not a setting."""
    return entry.model_dump(mode="json", exclude_none=True, exclude={"path"})


def _report(migrated: list[_RegistryEntry], dropped: list[_RegistryEntry], backup: Path) -> None:
    """What moved, what did not, and where the file that held it went.

    The list under each heading is the count, so the sentences do not carry
    one. The backup is named either way: a run that only drops tables still
    rewrites config.toml, and that is the run nobody asked for.

    Flushed, because `groundcrew daemon` migrates too and then runs until it is
    signalled. Python only flushes a block-buffered stdout at interpreter exit,
    so an unflushed report reaches the journal days later or never — and a
    dropped table's settings exist nowhere else.
    """
    lines = []
    if migrated:
        lines.append("migrated per-repo settings from config.toml to repos.toml:")
        lines += [f"  {e.path} — {', '.join(_settings(e)) or 'nothing set'}" for e in migrated]
    if dropped:
        lines.append("dropped per-repo settings for directories that are not registered:")
        for entry in dropped:
            held = ", ".join(f"{k} = {json.dumps(v)}" for k, v in _settings(entry).items())
            lines.append(f"  {entry.path} — {held or 'nothing set'}")
    lines.append(f"config.toml was rewritten; the original is {backup.name}")
    print("\n".join(lines), flush=True)


def migrate_config() -> None:
    """Move every [repos."<path>"] table into repos.toml, then take it out of config.toml."""
    path = config_dir() / "config.toml"
    try:
        text = path.read_text()
        data = tomllib.loads(text)
    except FileNotFoundError:
        return
    except tomllib.TOMLDecodeError:
        return  # `load` reports the parse error next, with the file named
    legacy = data.get("repos")
    if not legacy:
        return  # nothing to move; an empty `repos` key is `load`'s to name
    if not isinstance(legacy, dict):
        raise ConfigError('config.toml: repos must be [repos."<path>"] tables')

    # Everything that can fail happens before the first write. Copying a table
    # over and letting the registry loader reject it afterwards would cost the
    # user the settings and the config that held them, so one unusable table
    # aborts the migration with both files untouched.
    entries = [_entry(raw, table) for raw, table in legacy.items()]
    _reject_clashing_spellings(list(legacy), entries)
    cleaned = _strip_repo_tables(text)
    registry = load_registry()
    known = {entry.path: entry for entry in registry}
    migrated = [e for e in entries if e.path in known]
    # A table for a directory nobody registered is dropped, not registered:
    # expanding the supervised fleet during an upgrade would spawn supervisors
    # in directories the user may have removed on purpose. The report names it.
    dropped = [e for e in entries if e.path not in known]

    # The original goes first, then the settings, then the tables go away.
    # config.toml still holding them is what "not migrated yet" means, so a
    # crash short of the last write migrates the same input again next run.
    # ponytail: one window survives — crash after repos.toml, change that
    # path's settings, and the next run overwrites them from the stale table.
    # One path, one narrow window; every fix costs more than the failure.
    backup = path.with_name(path.name + ".bak")
    atomic_write(backup, text)
    if migrated:
        # save_registry keys on the path and takes the last write, so the
        # rebuilt entries replace the ones they were layered onto.
        save_registry(registry + [known[e.path].model_copy(update=_settings(e)) for e in migrated])
    atomic_write(path, cleaned)
    _report(migrated, dropped, backup)

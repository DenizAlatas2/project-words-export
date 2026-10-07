#!/usr/bin/env python3
"""Maintain a manually confirmed project glossary and export portable word lists."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import stat
import sys
import tomllib
import unicodedata
from pathlib import Path
from typing import Any

VERSION = "0.1.0"


def norm(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(unicodedata.normalize("NFKC", folded).split())


def tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text)
    chars: list[str] = []
    previous = ""
    for char in text:
        if previous and (previous.islower() or previous.isdigit()) and char.isupper():
            chars.append(" ")
        chars.append(char)
        previous = char
    text = "".join(chars)
    return re.findall(r"[^\W_]+", text, flags=re.UNICODE)


def _open_nofollow(path: Path, *, directory: bool = False) -> int:
    """Open an absolute path component by component without following symlinks."""
    if not (
        isinstance(getattr(os, "O_NOFOLLOW", None), int)
        and isinstance(getattr(os, "O_DIRECTORY", None), int)
        and os.open in os.supports_dir_fd
    ):
        raise OSError("descriptor-relative no-follow access is unavailable")
    absolute = path.absolute()
    parts = absolute.parts[1:]
    fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if not parts:
            if directory:
                return fd
            raise OSError("expected a file path")
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if index < len(parts) - 1 or directory:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def read_manifest(root: Path, spelling: str) -> Any:
    """Read a selected manifest through pinned no-follow directory descriptors."""
    relative = Path(spelling)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"selected source must stay inside project root: {spelling}")
    if not (
        isinstance(getattr(os, "O_NOFOLLOW", None), int)
        and isinstance(getattr(os, "O_DIRECTORY", None), int)
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    ):
        raise ValueError("descriptor-relative no-follow manifest access is unavailable on this platform")
    root_fd = _open_nofollow(root, directory=True)
    parent_fd = root_fd
    file_fd: int | None = None
    try:
        parts = [part for part in relative.parts if part not in ("", ".")]
        for part in parts[:-1]:
            before = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise ValueError(f"selected manifest crosses a non-directory or symlink: {spelling}")
            child_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
            opened = os.fstat(child_fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                os.close(child_fd)
                raise ValueError(f"selected manifest parent changed while opening: {spelling}")
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = child_fd

        name = parts[-1]
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"selected manifest is not a regular non-symlink file: {spelling}")
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"selected manifest changed while opening: {spelling}")
        with os.fdopen(file_fd, "rb", closefd=True) as stream:
            file_fd = None
            data = stream.read()
            after = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ValueError(f"selected manifest changed while being read: {spelling}")
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd != root_fd:
            os.close(parent_fd)
        os.close(root_fd)

    if Path(spelling).suffix.lower() == ".json":
        return json.loads(data.decode("utf-8"))
    if Path(spelling).suffix.lower() == ".toml":
        return tomllib.loads(data.decode("utf-8"))
    raise ValueError(f"unsupported manifest format: {Path(spelling).suffix}")


def field_value(document: Any, dotted: str) -> Any:
    value = document
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def selected_path(root: Path, spelling: str) -> Path:
    relative = Path(spelling)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"selected source must stay inside project root: {spelling}")
    path = root / relative
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"selected source crosses a symlink: {spelling}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"selected source is missing or unreadable: {spelling}") from exc
    if not resolved.is_relative_to(root) or not path.is_file():
        raise ValueError(f"selected source is not a regular file inside project root: {spelling}")
    return path


def discover(config_path: Path, glossary_path: Path) -> list[dict[str, Any]]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    root = (config_path.parent / config["root"]).resolve()
    files = config.get("files", [])
    manifests = config.get("manifests", {})
    if not isinstance(files, list) or not all(isinstance(p, str) for p in files):
        raise ValueError("config.files must be a list of selected file paths")
    if not isinstance(manifests, dict) or not all(isinstance(k, str) and isinstance(v, list) for k, v in manifests.items()):
        raise ValueError("config.manifests must map selected paths to dotted field names")
    collected: dict[str, dict[str, Any]] = {}

    def add(word: str, source: dict[str, str]) -> None:
        key = norm(word)
        if not key:
            return
        row = collected.setdefault(key, {"word": word, "sources": []})
        if source not in row["sources"]:
            row["sources"].append(source)

    for spelling in files:
        path = selected_path(root, spelling)
        for word in tokens(path.stem):
            add(word, {"path": spelling, "kind": "filename"})
    for spelling, fields in manifests.items():
        path = selected_path(root, spelling)
        doc = read_manifest(root, spelling)
        for field in fields:
            if not isinstance(field, str):
                raise ValueError(f"manifest field for {spelling} must be a string")
            value = field_value(doc, field)
            if isinstance(value, str):
                for word in tokens(value):
                    add(word, {"path": spelling, "kind": "manifest", "field": field})

    candidates = sorted(collected.values(), key=lambda item: (norm(item["word"]), item["word"]))
    for item in candidates:
        item["sources"].sort(key=lambda s: (s["path"], s["kind"], s.get("field", "")))
    if glossary_path.exists():
        glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
    else:
        glossary = {"schema_version": 1, "terms": [], "candidates": []}
    if glossary.get("schema_version") != 1 or not isinstance(glossary.get("terms"), list):
        raise ValueError("unsupported glossary schema; expected schema_version 1 and terms list")
    known = {norm(t["word"]): t for t in glossary["terms"] if isinstance(t, dict) and isinstance(t.get("word"), str)}
    active_sources = {norm(c["word"]): c["sources"] for c in candidates}
    for key, term in known.items():
        sources = term.setdefault("sources", [])
        observed = active_sources.get(key, [])
        old_keys = {(s.get("path"), s.get("kind"), s.get("field")) for s in sources if isinstance(s, dict)}
        observed_keys = {(s["path"], s["kind"], s.get("field")) for s in observed}
        for source in sources:
            if isinstance(source, dict):
                source["active"] = (source.get("path"), source.get("kind"), source.get("field")) in observed_keys
        for source in observed:
            skey = (source["path"], source["kind"], source.get("field"))
            if skey not in old_keys:
                sources.append({**source, "active": True})
    glossary["candidates"] = candidates
    glossary_path.parent.mkdir(parents=True, exist_ok=True)
    glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return candidates


def approved_terms(glossary: dict[str, Any]) -> list[dict[str, Any]]:
    if glossary.get("schema_version") != 1 or not isinstance(glossary.get("terms"), list):
        raise ValueError("unsupported glossary schema")
    terms = [t for t in glossary["terms"] if isinstance(t, dict) and t.get("confirmed") is True]
    if any(not isinstance(t.get("word"), str) for t in terms):
        raise ValueError("every confirmed term needs a string word")
    return terms


def validate_aliases(terms: list[dict[str, Any]]) -> None:
    owners: dict[str, str] = {}
    for term in terms:
        word = term["word"].strip()
        if not word or "\n" in word or "\r" in word or len(word) > 4000:
            raise ValueError(f"invalid confirmed word: {word!r}")
        if any(ch.isspace() for ch in word):
            raise ValueError(f"confirmed word must be one token: {word!r}")
        if not isinstance(term.get("sources"), list) or not term["sources"]:
            raise ValueError(f"confirmed word needs provenance: {word!r}")
        aliases = term.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            raise ValueError(f"aliases must be a list of strings for {word!r}")
        key = norm(word)
        if key in owners:
            raise ValueError(f"duplicate normalized term {word!r} conflicts with {owners[key]!r}")
        owners[key] = word
    for term in terms:
        word = term["word"].strip()
        for alias in term.get("aliases", []):
            alias = alias.strip()
            if not alias or len(alias) > 60:
                raise ValueError(f"invalid alias for {word!r}; Wispr Flow allows up to 60 characters")
            key = norm(alias)
            if key in owners:
                raise ValueError(f"alias {alias!r} conflicts with term {owners[key]!r}")
            owners[key] = word


def cspell_bytes(terms: list[dict[str, Any]]) -> bytes:
    words = {term["word"].strip() for term in terms}
    for term in terms:
        # CSpell's project dictionary is one entry per line. Phrase aliases are
        # retained for Wispr's correction CSV but cannot be represented safely.
        words.update(alias for alias in term.get("aliases", []) if not any(ch.isspace() for ch in alias))
    return ("".join(word + "\n" for word in sorted(words, key=lambda w: (norm(w), w)))).encode("utf-8")


def wispr_csv_bytes(terms: list[dict[str, Any]]) -> bytes:
    rows: list[tuple[str, ...]] = []
    for term in sorted(terms, key=lambda t: (norm(t["word"]), t["word"])):
        word = term["word"].strip()
        if len(word) > 60:
            raise ValueError(f"Wispr Flow dictionary word exceeds 60 characters: {word!r}")
        rows.append((word,))
        rows.extend((alias.strip(), word) for alias in sorted(term.get("aliases", []), key=lambda a: (norm(a), a)))
    import io
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def export(glossary_path: Path, cspell_path: Path, wispr_path: Path) -> None:
    glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
    terms = approved_terms(glossary)
    validate_aliases(terms)
    cspell_path.parent.mkdir(parents=True, exist_ok=True)
    wispr_path.parent.mkdir(parents=True, exist_ok=True)
    cspell_path.write_bytes(cspell_bytes(terms))
    wispr_path.write_bytes(wispr_csv_bytes(terms))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="project-words-export", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p_discover = sub.add_parser("discover", help="extract unconfirmed candidates from explicitly selected names")
    p_discover.add_argument("--config", type=Path, required=True)
    p_discover.add_argument("--glossary", type=Path, required=True)
    p_export = sub.add_parser("export", help="export manually confirmed entries")
    p_export.add_argument("--glossary", type=Path, required=True)
    p_export.add_argument("--cspell", type=Path, required=True)
    p_export.add_argument("--wispr-csv", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "discover":
            candidates = discover(args.config, args.glossary)
            print(f"{len(candidates)} Kandidaten vorgeschlagen; keiner davon wurde bestätigt.")
        else:
            export(args.glossary, args.cspell, args.wispr_csv)
            print(f"Exporte geschrieben: {args.cspell}, {args.wispr_csv}")
        return 0
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"project-words-export: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

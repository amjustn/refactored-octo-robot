"""Minimal YAML-subset parser for eval golden cases.

The managed runtime has no PyYAML. Golden case files use only this subset:

    key: scalar
    key: [inline, list]
    key:
      nested_key: scalar
      list_key:
        - item one
        - item two

Scalars: quoted/unquoted strings, int, float, true/false, null.
"""
from __future__ import annotations


def _parse_scalar(text: str):
    t = text.strip()
    if not t:
        return ""
    if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
        return t[1:-1]
    low = t.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~"):
        return None
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _parse_inline_list(text: str):
    inner = text.strip()[1:-1].strip()
    if not inner:
        return []
    parts = []
    buf = ""
    in_quote = None
    for ch in inner:
        if in_quote:
            buf += ch
            if ch == in_quote:
                in_quote = None
        elif ch in "\"'":
            in_quote = ch
            buf += ch
        elif ch == ",":
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    return [_parse_scalar(p) for p in parts]


def loads(text: str) -> dict:
    root = {}
    # stack of (indent, container)
    stack = [(-1, root)]

    for raw_line in text.split("\n"):
        if not raw_line.strip() or raw_line.strip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        container = stack[-1][1]

        if line.startswith("- "):
            item = _parse_scalar(line[2:])
            if not isinstance(container, list):
                # convert pending dict value into a list
                raise ValueError(f"list item without list parent: {raw_line}")
            container.append(item)
            continue

        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value == "":
                # block value: decide dict vs list by peeking next lines later;
                # default to dict, switch to list on first '- ' item
                new_container: object = {}
                container[key] = new_container
                stack.append((indent, new_container))
            elif value.startswith("["):
                container[key] = _parse_inline_list(value)
            else:
                container[key] = _parse_scalar(value)
            continue

        raise ValueError(f"unparseable line: {raw_line}")

    # second pass fix: dicts that should be lists — handled in load() below
    return root


def load(path: str) -> dict:
    """Parse a golden-case file, tolerating dict/list ambiguity for '- ' blocks."""

    text = open(path, encoding="utf-8").read()
    # Pre-pass: find keys whose block is a list (next non-empty line starts with '- ')
    lines = text.split("\n")
    list_keys = set()
    for i, raw in enumerate(lines):
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        if raw.rstrip().endswith(":"):
            indent = len(raw) - len(raw.lstrip(" "))
            for nxt in lines[i + 1:]:
                if not nxt.strip() or nxt.strip().startswith("#"):
                    continue
                nindent = len(nxt) - len(nxt.lstrip(" "))
                if nindent > indent and nxt.strip().startswith("- "):
                    list_keys.add(raw.strip()[:-1].strip())
                break

    root: dict = {}
    stack = [(-1, root)]
    for raw in lines:
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        container = stack[-1][1]

        if line.startswith("- "):
            if not isinstance(container, list):
                raise ValueError(f"list item under non-list: {raw}")
            container.append(_parse_scalar(line[2:]))
            continue

        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "":
            new_container = [] if key in list_keys else {}
            container[key] = new_container
            stack.append((indent, new_container))
        elif value.startswith("["):
            container[key] = _parse_inline_list(value)
        else:
            container[key] = _parse_scalar(value)

    return root

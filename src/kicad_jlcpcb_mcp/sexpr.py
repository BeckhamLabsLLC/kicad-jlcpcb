"""Minimal s-expression reader/writer for KiCad files.

KiCad's `.kicad_sch` and `.kicad_pcb` files are LISP-style nested lists
of atoms. This module gives us:

  - `parse(text)` → nested Python list (atoms are strings)
  - `dump(node)`  → text in KiCad's pretty-printed style
  - `find(node, name)` → first child list whose head is `name`
  - `find_all(node, name)` → all such children
  - `replace(node, name, new_child)` / `add(node, child)` for in-place patching

We do NOT try to be a full KiCad serializer. We round-trip enough
structure that schematic.py can build a valid `.kicad_sch` and the
existing project module can patch design rules. Comments are lost on
round-trip but KiCad's own files don't include comments anyway.
"""

from __future__ import annotations

from typing import Iterator

# A node is either a string atom or a list whose first element is a
# string (the head/keyword). Lists are written as `(head ...children)`.
Node = list  # type alias kept loose for readability


class SExprError(ValueError):
    """Parser hit malformed input."""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse(text: str) -> list:
    """Parse a KiCad-style s-expression string into nested Python lists.

    String atoms keep their quotes off; whitespace and quoted strings
    with embedded escapes are handled. Numbers stay as strings — KiCad's
    format doesn't distinguish int/float at parse time and we never need
    to do arithmetic on them in this module.
    """
    tokens = _tokenize(text)
    it = iter(tokens)
    try:
        first = next(it)
    except StopIteration:
        raise SExprError("Empty s-expression input")
    if first != "(":
        raise SExprError(f"Expected '(' at start, got {first!r}")
    result = _parse_list(it)
    # Allow trailing whitespace tokens but nothing else.
    leftover = list(it)
    if leftover:
        raise SExprError(f"Trailing tokens after root list: {leftover[:5]}...")
    return result


def _tokenize(text: str) -> list[str]:
    """Split text into parens, atoms, and quoted strings."""
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "()":
            tokens.append(ch)
            i += 1
            continue
        if ch == '"':
            # Quoted string with backslash escapes
            j = i + 1
            buf = ['"']
            while j < n:
                c = text[j]
                if c == "\\" and j + 1 < n:
                    buf.append(c)
                    buf.append(text[j + 1])
                    j += 2
                    continue
                buf.append(c)
                if c == '"':
                    j += 1
                    break
                j += 1
            else:
                raise SExprError("Unterminated quoted string")
            tokens.append("".join(buf))
            i = j
            continue
        # Bare atom: read until whitespace or paren
        j = i
        while j < n and not text[j].isspace() and text[j] not in "()":
            j += 1
        tokens.append(text[i:j])
        i = j
    return tokens


def _parse_list(it: Iterator[str]) -> list:
    """Parse the body of a list. Caller already consumed the opening '('."""
    items: list = []
    for tok in it:
        if tok == "(":
            items.append(_parse_list(it))
        elif tok == ")":
            return items
        else:
            items.append(_unquote(tok))
    raise SExprError("Unclosed list")


def _unquote(tok: str) -> str:
    """Strip surrounding quotes from a string atom; pass bare atoms through.

    Note: this returns a plain Python string for both quoted and bare
    atoms. The original quoted-vs-bare distinction is rebuilt at dump
    time based on whether the string needs quoting.
    """
    if len(tok) >= 2 and tok.startswith('"') and tok.endswith('"'):
        # Unescape \" and \\
        return tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return tok


# ---------------------------------------------------------------------------
# Dumper
# ---------------------------------------------------------------------------


def dump(node, *, indent: int = 0) -> str:
    """Render a parsed node back to KiCad-style text.

    Nested lists indent by 2 spaces per level. Lists with no nested
    children stay on one line; lists containing nested lists break their
    children onto separate lines.
    """
    if not isinstance(node, list):
        return _quote_if_needed(str(node))

    if not node:
        return "()"

    has_nested = any(isinstance(c, list) for c in node)
    head = _quote_if_needed(str(node[0]))
    pad = "  " * indent
    if not has_nested:
        body = " ".join(_quote_if_needed(str(c)) for c in node[1:])
        if body:
            return f"({head} {body})"
        return f"({head})"

    inner_pad = "  " * (indent + 1)
    parts = [f"({head}"]
    for child in node[1:]:
        parts.append("\n" + inner_pad + dump(child, indent=indent + 1))
    parts.append("\n" + pad + ")")
    return "".join(parts)


def _quote_if_needed(atom: str) -> str:
    """Add quotes around an atom if it contains whitespace or special chars.

    KiCad bare atoms are anything that doesn't need quoting; everything
    else gets wrapped in double quotes with `"` and `\\` escaped.
    """
    if not atom:
        return '""'
    if any(ch in atom for ch in ' \t\n\r"()'):
        escaped = atom.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return atom


# ---------------------------------------------------------------------------
# Convenience traversal helpers
# ---------------------------------------------------------------------------


def find(node, name: str):
    """Return the first child list of `node` whose head is `name`, or None."""
    if not isinstance(node, list):
        return None
    for child in node:
        if isinstance(child, list) and child and child[0] == name:
            return child
    return None


def find_all(node, name: str) -> list:
    """Return every child list of `node` whose head is `name`."""
    if not isinstance(node, list):
        return []
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def add(node, child) -> None:
    """Append `child` to `node` in place."""
    if not isinstance(node, list):
        raise SExprError("add() target must be a list")
    node.append(child)


def replace(node, name: str, new_child) -> bool:
    """Replace the first child list named `name` with `new_child`. Returns
    True if a replacement happened."""
    if not isinstance(node, list):
        return False
    for i, child in enumerate(node):
        if isinstance(child, list) and child and child[0] == name:
            node[i] = new_child
            return True
    return False

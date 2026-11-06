"""A small YAML reader for the policy files, so the code needs no YAML package.

Supports block maps, block lists (scalars or maps), one-line flow lists and maps, quoted
and plain scalars, comments and the `|` and `>` block scalars. It is not a full YAML parser:
no anchors, tags or multi-document files.
"""
from __future__ import annotations

import re
from typing import Any


class YamlError(ValueError):
    """The text is outside the supported subset."""


def _strip_comment(line: str) -> str:
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def _scalar(token: str) -> Any:
    token = token.strip()
    if token == "":
        return None
    if token[0] == "'" and token[-1] == "'" and len(token) > 1:
        return token[1:-1].replace("''", "'")
    if token[0] == '"' and token[-1] == '"' and len(token) > 1:
        return token[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if token[0] in "[{":
        return _flow(token)
    low = token.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~"):
        return None
    for cast in (int, float):
        try:
            return cast(token)
        except ValueError:
            pass
    return token


def _split_flow(inner: str) -> list[str]:
    parts, depth, quote, cur = [], 0, None, ""
    for ch in inner:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def _flow(token: str) -> Any:
    inner = token[1:-1].strip()
    if token[0] == "[":
        return [_scalar(p) for p in _split_flow(inner)]
    out: dict[str, Any] = {}
    for part in _split_flow(inner):
        key, _, rest = part.partition(":")
        out[key.strip()] = _scalar(rest)
    return out


def _key_value(content: str) -> tuple[str, str] | None:
    """Split `key: rest` where the colon is followed by a space or ends the line."""
    m = re.match(r"""^("[^"]*"|'[^']*'|[^\s:#'"][^:]*?)\s*:(?:\s+(.*)|)$""", content)
    if not m:
        return None
    return m.group(1).strip("\"'"), (m.group(2) or "").strip()


class _Reader:
    def __init__(self, text: str) -> None:
        self.raw = text.splitlines()
        self.lines: list[tuple[int, str, int]] = []
        for n, raw in enumerate(self.raw):
            s = _strip_comment(raw)
            if s.strip():
                self.lines.append((len(s) - len(s.lstrip(" ")), s.strip(), n))
        self.pos = 0

    def block(self, indent: int) -> Any:
        if self.lines[self.pos][1].startswith("- ") or self.lines[self.pos][1] == "-":
            return self.seq(indent)
        return self.mapping(indent)

    def seq(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while self.pos < len(self.lines):
            ind, content, _ = self.lines[self.pos]
            if ind != indent or not (content.startswith("- ") or content == "-"):
                break
            rest = content[1:].strip()
            if rest == "":
                self.pos += 1
                items.append(self.block(self.lines[self.pos][0]))
            elif _key_value(rest) and rest[0] not in "[{\"'":
                # a map whose first key sits on the dash line: re-read it at the key column
                ind2 = indent + (len(content) - len(content[1:].lstrip()))
                self.lines[self.pos] = (ind2, rest, self.lines[self.pos][2])
                items.append(self.mapping(ind2))
            else:
                self.pos += 1
                items.append(_scalar(rest))
        return items

    def mapping(self, indent: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while self.pos < len(self.lines):
            ind, content, n = self.lines[self.pos]
            if ind != indent or content.startswith("- "):
                break
            kv = _key_value(content)
            if kv is None:
                raise YamlError(f"line {n + 1}: cannot read {content!r}")
            key, rest = kv
            self.pos += 1
            if rest in ("|", ">", "|-", ">-"):
                out[key] = self.block_scalar(indent, n, rest[0] == "|")
            elif rest == "":
                nxt = self.lines[self.pos] if self.pos < len(self.lines) else None
                if nxt and (nxt[0] > indent or (nxt[0] == indent and nxt[1].startswith("- "))):
                    out[key] = self.block(nxt[0])
                else:
                    out[key] = None
            else:
                out[key] = _scalar(rest)
        return out

    def block_scalar(self, indent: int, start: int, literal: bool) -> str:
        """Collect raw lines indented deeper than the key; skip them in the line list."""
        body: list[str] = []
        end = start + 1
        while end < len(self.raw):
            line = self.raw[end]
            if line.strip() and (len(line) - len(line.lstrip(" "))) <= indent:
                break
            body.append(line)
            end += 1
        while self.pos < len(self.lines) and self.lines[self.pos][2] < end:
            self.pos += 1
        pad = min((len(b) - len(b.lstrip(" ")) for b in body if b.strip()), default=0)
        lines = [b[pad:] for b in body]
        return ("\n" if literal else " ").join(lines).strip()


def load(text: str) -> Any:
    reader = _Reader(text)
    if not reader.lines:
        return {}
    return reader.block(reader.lines[0][0])

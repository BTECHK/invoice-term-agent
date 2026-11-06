#!/usr/bin/env python3
"""PreToolUse hook: deny by default against policy/tool-policy.yaml, and audit every call.

What it does
- Reads the tool call Claude Code is about to make (JSON on stdin).
- Works out the role (subagent identity, then AP_AGENT_ROLE, then the policy default).
- Denies the call unless the role's allowed_tools list names it. The three ERP write tools
  and Call Assistant Model are denied for every role. Write and Edit stay inside write_paths
  and outside protected_paths. Secrets files cannot be read.
- Appends one JSON line per call to audit/hooks.jsonl (fields of schemas/audit-event.json).
  Tool input is stored as a SHA-256 hash only, never as text.

What it never does
- It never returns "allow". It can only narrow what .claude/settings.json already permits, so a
  bug here cannot open a door. A call it does not object to falls through to the normal
  permission flow.
- It fails closed. An unreadable policy, unreadable input or an unwritable audit log denies.

Development mode: a person sets AP_AGENT_DEV=1 before launching the coding agent. Built-in tools
open up, but the ERP read limits and the hook log protection stay.
Standard library only. PyYAML is used when installed; otherwise a small reader handles the
YAML subset that policy/*.yaml is restricted to.
"""
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
POLICY_FILE = PROJECT / "policy" / "tool-policy.yaml"
FILE_TOOLS_READ = {"Read"}
FILE_TOOLS_WRITE = {"Write", "Edit", "NotebookEdit"}


# ---------------------------------------------------------------- policy reader

def _strip_comment(line):
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


def _scalar(token):
    token = token.strip()
    if token == "":
        return None
    if len(token) >= 2 and token[0] in "\"'" and token[-1] == token[0]:
        return token[1:-1]
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


def _flow_list(token):
    inner = token.strip()[1:-1].strip()
    return [] if inner == "" else [_scalar(part) for part in inner.split(",")]


def _block(lines, pos, indent):
    if lines[pos][1].startswith("- "):
        items = []
        while pos < len(lines) and lines[pos][0] == indent and lines[pos][1].startswith("- "):
            items.append(_scalar(lines[pos][1][2:]))
            pos += 1
        return items, pos
    mapping = {}
    while pos < len(lines) and lines[pos][0] == indent and not lines[pos][1].startswith("- "):
        key, _, rest = lines[pos][1].partition(":")
        rest = rest.strip()
        pos += 1
        if rest == "":
            nxt = lines[pos] if pos < len(lines) else None
            if nxt and (nxt[0] > indent or (nxt[0] == indent and nxt[1].startswith("- "))):
                value, pos = _block(lines, pos, nxt[0])
            else:
                value = None
        elif rest.startswith("["):
            value = _flow_list(rest)
        else:
            value = _scalar(rest)
        mapping[key.strip()] = value
    return mapping, pos


def mini_yaml(text):
    lines = []
    for raw in text.splitlines():
        stripped = _strip_comment(raw)
        if stripped.strip():
            lines.append((len(stripped) - len(stripped.lstrip(" ")), stripped.strip()))
    return _block(lines, 0, lines[0][0])[0]


def load_policy():
    text = POLICY_FILE.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except ImportError:
        return mini_yaml(text)
    return yaml.safe_load(text)


# ---------------------------------------------------------------- decision

def canonical(tool, policy):
    """Map an alias server (the local mock) to the canonical ERP server name."""
    if tool.startswith("mcp__"):
        parts = tool.split("__", 2)
        if len(parts) == 3:
            known = [policy["mcp"]["server"], *(policy["mcp"].get("server_aliases") or [])]
            if parts[1] in known:
                return "mcp__%s__%s" % (policy["mcp"]["server"], parts[2])
    return tool


def resolve_role(policy, event):
    rr = policy["role_resolution"]
    dev = os.environ.get(policy["development_mode"]["env_var"]) == "1"
    by_agent = (rr.get("subagent_roles") or {}).get(str(event.get("agent_type") or ""))
    role = by_agent or os.environ.get(rr["env_var"]) or rr["default_role"]
    return role, dev


def resolve_path(raw, event):
    path = Path(str(raw))
    if not path.is_absolute():
        path = Path(event.get("cwd") or PROJECT) / path
    return path.resolve()


def rel_posix(path):
    try:
        return path.relative_to(PROJECT).as_posix()
    except ValueError:
        return None


def under(rel, entry):
    # Casefolded: macOS and Windows volumes treat Policy/ and policy/ as one folder.
    rel, entry = rel.casefold(), entry.casefold()
    return rel.startswith(entry) if entry.endswith("/") else rel == entry


def secrets_name(name, policy):
    """A file name the Read tool must never open: the listed names, and any .env* except .env.example."""
    folded = name.casefold()
    listed = {entry.casefold() for entry in (policy.get("read_deny_names") or [])}
    return folded in listed or (folded.startswith(".env") and folded != ".env.example")


def file_decision(policy, spec, tool, tool_input, event, dev):
    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
    if not raw:
        return False, "file tool call without a path"
    path = resolve_path(raw, event)
    rel = rel_posix(path)
    if tool in FILE_TOOLS_READ and secrets_name(path.name, policy):
        return False, "secrets file cannot be read"
    if rel is None:
        if dev and tool in FILE_TOOLS_READ:
            return True, "development mode read outside project"
        return False, "path is outside the project"
    if tool in FILE_TOOLS_WRITE:
        if rel.casefold() == policy["hook_log"].casefold():
            return False, "the hook log is append only and written by the hook"
        if dev:
            return True, "development mode write"
        if any(under(rel, p) for p in policy["protected_paths"]):
            return False, "protected path"
        if not any(under(rel, p) for p in (spec.get("write_paths") or [])):
            return False, "path is not in write_paths for this role"
    return True, "ok"


def decide(policy, role, dev, tool, tool_input, event):
    tool = canonical(tool, policy)
    if tool in policy["denied_everywhere"]:
        return False, "write tool denied in every environment (ADR-003)"
    if tool in (policy.get("not_enabled_in_pilot") or []):
        return False, "tool not enabled in the pilot"
    roles = policy["roles"]
    if dev:
        spec = roles["orchestrator"]
        if tool.startswith("mcp__") and tool not in spec["allowed_tools"]:
            return False, "ERP tool not on the read list"
    else:
        spec = roles.get(role)
        if spec is None:
            return False, "unknown role '%s'" % role[:40]
        if tool in spec["denied_tools"]:
            return False, "tool denied for role %s" % role
        if tool not in spec["allowed_tools"]:
            return False, "tool not on the allowlist for role %s (deny by default)" % role
    if tool in FILE_TOOLS_READ | FILE_TOOLS_WRITE:
        return file_decision(policy, spec, tool, tool_input, event, dev)
    return True, "ok"


# ---------------------------------------------------------------- audit

def write_audit(policy, role, dev, tool, tool_input, allowed, note):
    digest = hashlib.sha256(
        json.dumps(tool_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    event = {
        "event_id": str(uuid.uuid4()),
        "invoice_id": os.environ.get(policy["role_resolution"]["invoice_env_var"]) or "unscoped",
        "role": "person" if dev else (role if role in policy["roles"] else policy["role_resolution"]["default_role"]),
        "tool": tool,
        "allowed": allowed,
        "input_hash": digest,
        "output_hash": None,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "note": note[:500],
    }
    log = PROJECT / policy["hook_log"]
    log.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Blocked by policy/tool-policy.yaml: " + reason,
        }
    }))


def main():
    try:
        event = json.load(sys.stdin)
        tool = str(event["tool_name"])
        tool_input = event.get("tool_input") or {}
    except Exception:
        deny("hook input could not be read")
        return
    try:
        policy = load_policy()
        role, dev = resolve_role(policy, event)
        allowed, note = decide(policy, role, dev, tool, tool_input, event)
        if canonical(tool, policy) != tool:
            note += " (alias server %s)" % tool.split("__")[1]
        write_audit(policy, role, dev, canonical(tool, policy), tool_input, allowed, note)
    except Exception as exc:  # fail closed, without echoing details into the transcript
        deny("hook error (%s), failing closed" % type(exc).__name__)
        return
    if not allowed:
        deny(note)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Repository lints. Prints every problem with its file path and exits 1 if there is any.

    python3 scripts/check.py            # a folder that does not exist yet is skipped, with a message
    python3 scripts/check.py --strict   # a skipped check is a problem

Checks: skills frontmatter, JSON Schema files, skills mirrors, tool policy against settings, tool contract and
model pins, hook wiring, deny list and agent limits, secret patterns, em and en dashes. PyYAML is used when
installed, a built-in reader otherwise.
"""
import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_KEYS = {"name", "description", "license", "compatibility", "metadata"}
UC2_SKILLS = ("no-validated-term", "over-60-days", "missing-or-wrong-po", "po-not-approved-or-receipted",
              "supplier-or-remittance-mismatch", "discount-eligibility", "value-outside-tolerance")
WRITE_TOOLS = ("triggererpaction", "startworkflow", "resumeworkflow")
SCHEMA_FILES = ("extraction.json", "proposal.json", "worklist-record.json", "audit-event.json")
DRAFT = "https://json-schema.org/draft/2020-12/schema"
TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")
DASHES = {chr(0x2014): "em dash", chr(0x2013): "en dash"}
SECRETS = {"static key header": r"(?:ApiKey|X-Api-Key|AppKey)\s*[:=]?\s*[A-Za-z0-9._~+/=-]{16,}", "bearer token": r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}",
           "GitHub token": r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,}", "AWS access key": r"AKIA[0-9A-Z]{16}",
           "private key header": r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"}
ALLOW = r"allow(ed)?([_-]?tools)?"
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
HOOK = ".claude/hooks/pre-tool-use.py"


# ---- small YAML reader, used only when PyYAML is not installed: block maps and lists, flow lists, no hash sign inside a value ----

def _split_comment(line):
    return re.sub(r"(^|\s)#.*$", "", line)


def _scalar(s):
    s = s.strip()
    if len(s) > 1 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        return [_scalar(x) for x in re.findall(r"\"[^\"]*\"|'[^']*'|[^,\s][^,]*", s[1:-1])]
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d*\.\d+", s):
        return float(s)
    return {"true": True, "false": False, "null": None, "~": None, "": None}.get(s, s)


def _block(lines, i, indent):
    if lines[i][1].startswith("-"):
        out = []
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("-"):
            body = lines[i][1][1:].lstrip()
            col = indent + len(lines[i][1]) - len(body)
            if re.match(r"^(\"[^\"]*\"|'[^']*'|[^\s\"'\[{][^:]*?)\s*:(\s|$)", body):
                lines[i] = (col, body)
                value, i = _block(lines, i, col)
            else:
                value, i = _scalar(body), i + 1
            out.append(value)
        return out, i
    out = {}
    while i < len(lines) and lines[i][0] == indent:
        m = re.match(r"^(\"[^\"]*\"|'[^']*'|[^:]+?)\s*:(?:\s+(.*))?$", lines[i][1])
        if not m:
            raise ValueError(f"cannot read line {lines[i][1]!r}")
        key, val, i = m.group(1).strip("\"'"), (m.group(2) or "").strip(), i + 1
        nested = i < len(lines) and (lines[i][0] > indent or (lines[i][0] == indent and lines[i][1].startswith("-")))
        if val[:1] in ("|", ">"):
            parts = []
            while i < len(lines) and lines[i][0] > indent:
                parts.append(lines[i][1])
                i += 1
            out[key] = " ".join(parts)
        elif val == "" and nested:
            out[key], i = _block(lines, i, lines[i][0])
        else:
            out[key] = _scalar(val)
    return out, i


def yaml_load(text):
    try:
        import yaml
    except ImportError:
        lines = [(len(l) - len(l.lstrip()), l.strip()) for l in map(_split_comment, text.splitlines()) if l.strip()]
        return _block(lines, 0, lines[0][0])[0] if lines else {}
    return yaml.safe_load(text)


# ---- checks: each returns a list of (path, message) and may append a reason to skips ----

def skill_problems(f, name):
    lines = f.read_text(encoding="utf-8").splitlines()
    ends = [i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"]
    out = [f"{len(lines)} lines, limit is 120"] if len(lines) > 120 else []
    if not lines or lines[0].strip() != "---" or not ends:
        return out + ["no frontmatter block between --- lines"]
    try:
        fm = yaml_load("\n".join(lines[1:ends[0]]))
    except Exception as exc:  # PyYAML and the built-in reader raise different error types
        return out + [f"frontmatter does not parse: {exc}"]
    fm = fm if isinstance(fm, dict) else {}
    out += [f"frontmatter field {k} is not in the spec ({', '.join(sorted(SPEC_KEYS))})" for k in sorted(set(fm) - SPEC_KEYS)]
    n, d, meta = fm.get("name"), fm.get("description"), fm.get("metadata", {})
    if n != name or not isinstance(n, str) or not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", n) or len(n) > 64:
        out.append(f"name {n!r} must equal the folder name {name!r}: lowercase letters, digits, single hyphens, at most 64")
    if not isinstance(d, str) or not 1 <= len(d) <= 1024:
        out.append("description is required and at most 1024 characters")
    if not isinstance(meta, dict) or not all(isinstance(v, str) for v in meta.values()):
        out.append("metadata must map string keys to string values (quote numbers)")
    return out


def check_skills(root, skips):
    base, out = root / "skills", []
    if not base.is_dir():
        skips.append("skills/ not found, skills frontmatter not checked")
        return out
    folders = sorted(p.name for p in base.iterdir() if p.is_dir())
    used, expected = set(UC2_SKILLS), root / "evals" / "golden" / "expected.json"
    if expected.is_file():
        used |= {s for e in json.loads(expected.read_text(encoding="utf-8"))["invoices"].values() for s in e["exceptions"]}
    out += [(f"skills/{s}", "skill folder is missing (UC-2 list or evals/golden/expected.json)") for s in sorted(used - set(folders))]
    for name in folders:
        f = base / name / "SKILL.md"
        if not f.is_file():
            out.append((f"skills/{name}", "folder has no SKILL.md"))
        else:
            out += [(f"skills/{name}/SKILL.md", m) for m in skill_problems(f, name)]
    return out


def schema_node(s, where, doc, out, data=True):
    """Structural check of one JSON Schema 2020-12 node. data is False for if, then, else, not and allOf style overlays."""
    if s is True or s is False:
        return
    if not isinstance(s, dict):
        return out.append(f"{where}: schema node must be an object")
    types = s["type"] if isinstance(s.get("type"), list) else [s["type"]] if "type" in s else []
    out += [f"{where}: unknown type {t!r}" for t in types if t not in TYPES]
    if data and ("object" in types or "properties" in s) and s.get("additionalProperties") is not False:
        out.append(f"{where}: object schema must set additionalProperties to false")
    props, req = s["properties"] if isinstance(s.get("properties"), dict) else {}, s.get("required", [])
    if not isinstance(req, list) or not all(isinstance(r, str) for r in req) or len(set(req)) != len(req):
        out.append(f"{where}: required must be a list of unique strings")
    elif props:
        out += [f"{where}: required property {r} is not declared in properties" for r in req if r not in props]
    subs = [(f"{where}.properties.{k}", v, True) for k, v in props.items()] + [(f"{where}.$defs.{k}", v, True) for k, v in (s.get("$defs") or {}).items()]
    subs += [(f"{where}.{k}", s[k], k == "items") for k in ("items", "not", "if", "then", "else", "contains") if k in s]
    for k in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if k in s and (not isinstance(s[k], list) or not s[k]):
            out.append(f"{where}: {k} must be a non empty list")
        elif k in s:
            subs += [(f"{where}.{k}[{i}]", v, k == "prefixItems") for i, v in enumerate(s[k])]
    for path, sub, is_data in subs:
        schema_node(sub, path, doc, out, is_data)
    out += [f"{where}: enum must be a non empty list"] * ("enum" in s and not (isinstance(s["enum"], list) and s["enum"]))
    try:
        re.compile(s.get("pattern", ""))
    except (re.error, TypeError):
        out.append(f"{where}: pattern is not a valid regular expression")
    if "$ref" in s:
        node = doc if str(s["$ref"]).startswith("#") else None
        for part in [p for p in str(s["$ref"])[2:].split("/") if p] if node is not None else []:
            node = node.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(node, dict) else None
        if node is None:
            out.append(f"{where}: $ref {s['$ref']} does not resolve inside this file")


def check_schemas(root, skips):
    base, out = root / "schemas", []
    if not base.is_dir():
        skips.append("schemas/ not found, schemas not checked")
        return out
    out += [(f"schemas/{n}", "expected schema file is missing") for n in SCHEMA_FILES if not (base / n).is_file()]
    for f in sorted(base.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            out.append((f"schemas/{f.name}", f"not valid JSON: {exc}"))
            continue
        found = [] if isinstance(doc, dict) and doc.get("$schema") == DRAFT else [f"$schema must be {DRAFT}"]
        schema_node(doc, "$", doc, found)
        out += [(f"schemas/{f.name}", m) for m in found]
    return out


def check_mirrors(root, skips):
    spec = importlib.util.spec_from_file_location("sync_skills", Path(__file__).with_name("sync-skills.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    exists, found = mod.drift(root)
    if not exists:
        skips.append("skills/ not found, mirrors not compared")
    return [(f"{m}/{rel}", f"{kind} compared with skills/; run python3 scripts/sync-skills.py") for kind, m, rel in found]


def norm_tool(t):
    t = str(t).strip()
    return re.sub(r"[^a-z0-9]", "", (t.split("__", 2)[-1] if t.startswith("mcp__") else t).lower())


def collect(node, pattern, out):  # string items of every list whose key matches pattern, at any depth
    for k, v in (node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()):
        if isinstance(node, dict) and re.fullmatch(pattern, str(k), re.I) and isinstance(v, list):
            out += [x for x in v if isinstance(x, str)]
        collect(v, pattern, out)
    return out


def check_policy(root, skips):
    """Tool policy against .claude/settings.json, the integration tool contract and the model pins."""
    pol, cfg, contract, models = (root / x for x in ("policy/tool-policy.yaml", ".claude/settings.json", "integration/agent-tools.yaml", "policy/models.yaml"))
    if not pol.is_file():
        skips.append("policy/tool-policy.yaml not found, tool policy not compared")
        return []
    out = []
    try:
        tree = yaml_load(pol.read_text(encoding="utf-8"))
        allowed, everywhere = collect(tree, ALLOW, []), {norm_tool(x) for x in collect(tree, r"den(y|ied)[_-]everywhere", [])}
        perms = json.loads(cfg.read_text(encoding="utf-8")).get("permissions", {}) if cfg.is_file() else None
        tools = yaml_load(contract.read_text(encoding="utf-8"))["tools"] if contract.is_file() else None
        pins = ((yaml_load(models.read_text(encoding="utf-8")) if models.is_file() else None) or {}).get("pinning", {}).get("pins")
    except Exception as exc:  # unreadable YAML or JSON
        return [("policy/tool-policy.yaml", f"cannot compare policy files: {exc}")]
    if not allowed:
        return [("policy/tool-policy.yaml", "no allowed tools found (looked for lists named allow, allowed or allowed_tools)")]
    mcp = lambda items: {norm_tool(x) for x in items if x.startswith("mcp__")}
    for t in WRITE_TOOLS:
        out += [("policy/tool-policy.yaml", f"a role is allowed write tool {a}") for a in allowed if norm_tool(a) == t]
        out += [("policy/tool-policy.yaml", f"denied_everywhere must list write tool {t}")] * (t not in everywhere)
    if perms is None:
        skips.append(".claude/settings.json not found, allowlist not compared")
    else:
        s_allow = [str(x) for x in perms.get("allow", [])]
        raw = {norm_tool(x): x for x in allowed + s_allow}
        # ERP tools must match exactly; built-in tools differ per role (the hook enforces them), so settings may list fewer.
        out += [(".claude/settings.json", f"permissions.allow is missing policy tool {raw[t]}") for t in sorted(mcp(allowed) - mcp(s_allow))]
        out += [(".claude/settings.json", f"permissions.allow has {raw[t]}, which no policy role allows") for t in sorted({norm_tool(x) for x in s_allow} - {norm_tool(x) for x in allowed})]
    if tools is None:
        skips.append("integration/agent-tools.yaml not found, contract tools not compared")
    elif {norm_tool(t["mcp_name"]) for t in tools if t.get("pilot") == "allowed"} != mcp(allowed):
        out.append(("integration/agent-tools.yaml", "tools marked allowed differ from the ERP tools the policy allows"))
    for role, pin in (pins or {}).items():
        if not re.search(r"\d", str(pin)) or re.search(r"latest|stable|current|default|alias", str(pin), re.I):
            out.append(("policy/models.yaml", f"pin for {role} is {pin!r}: pin one exact version, never a floating alias"))
    return out


def hook_problems(root, settings):
    """The PreToolUse hook is the enforcement for Claude Code, so it must be wired in and present."""
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    wired = any(
        isinstance(entry, dict) and entry.get("matcher") == "*" and any(
            isinstance(h, dict) and h.get("type") == "command" and HOOK in str(h.get("command", ""))
            for h in entry.get("hooks") or [])
        for entry in hooks.get("PreToolUse") or [])
    out = [] if wired else [(".claude/settings.json", f"hooks.PreToolUse needs a matcher * entry whose command runs {HOOK}")]
    return out + ([] if (root / HOOK).is_file() else [(HOOK, "hook file is missing")])


def agent_problems(root, tree, skips):
    """Each agent file stays inside the policy role it maps to: a subset of its tools and no more turns than its steps."""
    base = root / ".claude" / "agents"
    if not base.is_dir():
        skips.append(".claude/agents/ not found, agent limits not compared")
        return []
    roles, mapping = tree.get("roles") or {}, (tree.get("role_resolution") or {}).get("subagent_roles") or {}
    out = []
    for f in sorted(base.glob("*.md")):
        rel, lines = f".claude/agents/{f.name}", f.read_text(encoding="utf-8").splitlines()
        ends = [i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"]
        if not lines or lines[0].strip() != "---" or not ends:
            out.append((rel, "no frontmatter block between --- lines"))
            continue
        try:
            fm = yaml_load("\n".join(lines[1:ends[0]]))
        except Exception as exc:  # PyYAML and the built-in reader raise different error types
            out.append((rel, f"frontmatter does not parse: {exc}"))
            continue
        fm = fm if isinstance(fm, dict) else {}
        name = str(fm.get("name") or f.stem)
        role, spec = mapping.get(name), roles.get(mapping.get(name))
        if not isinstance(spec, dict):
            out.append((rel, f"agent {name!r} is not mapped to a policy role in role_resolution.subagent_roles"))
            continue
        raw = fm.get("tools")
        listed = [str(t).strip() for t in raw] if isinstance(raw, list) else [t.strip() for t in str(raw or "").split(",") if t.strip()]
        out += [(rel, "tools must list the agent's tools; an empty list inherits every tool")] * (not listed)
        out += [(rel, f"tool {t} is not allowed for policy role {role}") for t in sorted(set(listed) - set(spec.get("allowed_tools") or []))]
        turns, steps = fm.get("maxTurns"), spec.get("max_steps")
        if not isinstance(turns, int) or isinstance(turns, bool) or not isinstance(steps, int) or turns > steps:
            out.append((rel, f"maxTurns {turns!r} must be a number no larger than max_steps {steps!r} of policy role {role}"))
    return out


def check_enforcement(root, skips):
    """The hook is wired in, settings deny every tool the policy refuses, and agent files stay inside their roles."""
    pol, cfg = root / "policy" / "tool-policy.yaml", root / ".claude" / "settings.json"
    if not pol.is_file():
        skips.append("policy/tool-policy.yaml not found, hook, deny list and agent limits not compared")
        return []
    try:
        tree = yaml_load(pol.read_text(encoding="utf-8"))
        settings = json.loads(cfg.read_text(encoding="utf-8")) if cfg.is_file() else None
    except Exception as exc:  # unreadable YAML or JSON
        return [("policy/tool-policy.yaml", f"cannot compare enforcement files: {exc}")]
    out = agent_problems(root, tree, skips)
    if settings is None:
        skips.append(".claude/settings.json not found, hook wiring and deny list not checked")
        return out
    refused = collect(tree, r"den(y|ied)[_-]everywhere", []) + collect(tree, r"not[_-]enabled[_-]in[_-]pilot", [])
    deny = {norm_tool(x) for x in (settings.get("permissions") or {}).get("deny", [])}
    out += [(".claude/settings.json", f"permissions.deny must contain {t}") for t in refused if norm_tool(t) not in deny]
    return out + hook_problems(root, settings)


def check_text(root, skips):
    out = []
    for f in sorted(p for p in root.rglob("*") if p.is_file() and not SKIP_DIRS & set(p.relative_to(root).parts)):
        rel = f.relative_to(root).as_posix()
        out += [(rel, f"{name} in file name") for ch, name in DASHES.items() if ch in f.name]
        data = f.read_bytes() if f.stat().st_size <= 5_000_000 else b"\0"
        try:
            text = "" if b"\0" in data[:8192] else data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            out += [(f"{rel}:{n}", f"{name}, use a comma, colon, period or parentheses") for ch, name in DASHES.items() if ch in line]
            out += [(f"{rel}:{n}", f"possible secret ({kind}), value not shown") for kind, rx in SECRETS.items() if re.search(rx, line)]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Lint the repository. Exits 1 on any problem.")
    ap.add_argument("--strict", action="store_true", help="a skipped check counts as a problem")
    ap.add_argument("--root", default=str(ROOT), help="repository root (default: the folder above scripts/)")
    args = ap.parse_args(argv)
    root, skips, problems = Path(args.root), [], []
    checks = (("skills frontmatter", check_skills), ("schemas", check_schemas), ("skills mirrors", check_mirrors),
              ("tool policy, settings, contract, pins", check_policy),
              ("hook, deny list, agent limits", check_enforcement), ("secrets and dashes", check_text))
    for name, fn in checks:
        found = fn(root, skips)
        print(f"{'FAIL' if found else 'ok  '} {name}" + (f" ({len(found)})" if found else ""))
        problems += found
    for msg in skips:
        print(f"skip {msg}")
        problems += [("check.py", f"skipped in strict mode: {msg}")] if args.strict else []
    for path, msg in sorted(set(problems)):
        print(f"{path}: {msg}")
    print(f"{len(checks)} checks, {len(skips)} skipped, {len(set(problems))} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

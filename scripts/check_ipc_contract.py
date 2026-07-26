#!/usr/bin/env python3
"""Check that every `invoke<T>("cmd", args)` in the frontend agrees with the
Rust `#[tauri::command]` it calls.

Why this exists: TypeScript takes the `invoke<T>` type parameter at its word.
Nothing verifies it against the Rust signature, and nothing in the test suite
crosses the IPC boundary. In v0.0.62 `start_label_server` returned
`Result<u16, String>` while the frontend read it as `invoke<string>`, so a port
number was treated as a URL and `.includes()` blew up at the very end of a
completed batch. tsc was clean and 71 tests passed.

Checks, per call site:
  1. the command name exists in Rust
  2. the declared TS return type is compatible with the Rust return type
  3. every argument key passed matches a Rust parameter (camelCase <-> snake_case)
  4. no required Rust parameter is missing

Exits non-zero with a readable report on any mismatch.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUST_DIR = ROOT / "app" / "src-tauri" / "src"
TS = ROOT / "app" / "src" / "main.ts"

# Rust parameters Tauri injects; never supplied by the caller.
INJECTED = {"app", "window", "webview", "state", "app_handle"}

# What each Rust return type is allowed to be read as in TypeScript. `unknown`
# is always allowed (an untyped invoke asserts nothing).
COMPATIBLE: dict[str, set[str]] = {
    "i8": {"number"}, "i16": {"number"}, "i32": {"number"}, "i64": {"number"},
    "u8": {"number"}, "u16": {"number"}, "u32": {"number"}, "u64": {"number"},
    "usize": {"number"}, "f32": {"number"}, "f64": {"number"},
    "String": {"string"}, "&str": {"string"},
    "bool": {"boolean"},
    "()": {"void", "unknown", "null"},
    "Vec<String>": {"string[]", "Array<string>"},
    "Option<String>": {"string | null", "string | undefined", "string|null"},
}


def rust_commands(src: str) -> dict[str, dict]:
    """{name: {"ret": rust type, "params": [names], "optional": {names}}}"""
    out: dict[str, dict] = {}
    for m in re.finditer(r"#\[tauri::command\]\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*\(", src):
        name = m.group(1)
        # balance parens to capture the full signature
        i = m.end() - 1
        depth, j = 0, i
        while j < len(src):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        params_raw = src[i + 1 : j]
        tail = src[j + 1 : src.find("{", j)]

        ret = "()"
        rm = re.search(r"->\s*(.+?)\s*$", tail.strip())
        if rm:
            ret = rm.group(1).strip()
        # Result<T, E> -> T ; the error arm surfaces as a rejected promise
        rr = re.match(r"Result\s*<(.+)>$", ret)
        if rr:
            inner = rr.group(1)
            depth, cut = 0, len(inner)
            for k, ch in enumerate(inner):
                if ch == "<":
                    depth += 1
                elif ch == ">":
                    depth -= 1
                elif ch == "," and depth == 0:
                    cut = k
                    break
            ret = inner[:cut].strip()

        params, optional = [], set()
        depth, cur = 0, ""
        for ch in params_raw + ",":
            if ch in "<(":
                depth += 1
            elif ch in ">)":
                depth -= 1
            if ch == "," and depth == 0:
                p = cur.strip()
                cur = ""
                if not p:
                    continue
                pm = re.match(r"(?:mut\s+)?(\w+)\s*:\s*(.+)$", p, re.S)
                if not pm:
                    continue
                pname, ptype = pm.group(1), pm.group(2).strip()
                if pname in INJECTED or "AppHandle" in ptype or "Window" in ptype or "State<" in ptype:
                    continue
                params.append(pname)
                if ptype.startswith("Option<"):
                    optional.add(pname)
            else:
                cur += ch
        out[name] = {"ret": ret, "params": params, "optional": optional}
    return out


def snake(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def ts_call_sites(src: str) -> list[dict]:
    """Every invoke call, with its declared type and the arg keys it passes."""
    sites: list[dict] = []
    for m in re.finditer(r"invoke\s*(?:<([^>]*)>)?\s*\(\s*\"(\w+)\"", src):
        declared = (m.group(1) or "unknown").strip()
        name = m.group(2)
        line = src[: m.start()].count("\n") + 1

        # capture the object literal argument, if present
        rest = src[m.end() :]
        keys: list[str] = []
        am = re.match(r"\s*,\s*\{", rest)
        if am:
            depth, k = 0, rest.find("{")
            end = k
            while end < len(rest):
                if rest[end] == "{":
                    depth += 1
                elif rest[end] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                end += 1
            body = rest[k + 1 : end]
            # Split on TOP-LEVEL commas, then take the key from each segment.
            # Matching identifiers by lookahead picked up the value half of
            # `scope: currentPath ?? null`.
            segs, depth, cur = [], 0, ""
            for ch in body + ",":
                if ch in "{[(":
                    depth += 1
                elif ch in "}])":
                    depth -= 1
                if ch == "," and depth == 0:
                    segs.append(cur)
                    cur = ""
                else:
                    cur += ch
            for seg in segs:
                seg = seg.strip()
                if not seg or seg.startswith("..."):
                    continue
                key = seg.split(":", 1)[0].strip() if ":" in seg else seg
                if re.fullmatch(r"[A-Za-z_$][\w$]*", key):
                    keys.append(key)
        sites.append({"name": name, "declared": declared, "keys": keys, "line": line})
    return sites


def compatible(rust_ret: str, ts: str) -> bool:
    if ts in ("unknown", "any"):
        return True
    allowed = COMPATIBLE.get(rust_ret)
    if allowed is None:
        # Custom serde struct: any object-ish annotation is accepted, but a
        # primitive one is almost certainly a mistake.
        return ts not in {"string", "number", "boolean"}
    return ts.replace(" ", "") in {a.replace(" ", "") for a in allowed}


def main() -> int:
    rust_src = "\n".join(
        p.read_text() for p in sorted(RUST_DIR.rglob("*.rs"))
    )
    cmds = rust_commands(rust_src)
    sites = ts_call_sites(TS.read_text())
    problems: list[str] = []

    for s in sites:
        cmd = cmds.get(s["name"])
        if cmd is None:
            problems.append(
                f"main.ts:{s['line']}  invoke(\"{s['name']}\") has no "
                f"#[tauri::command] with that name"
            )
            continue

        if not compatible(cmd["ret"], s["declared"]):
            problems.append(
                f"main.ts:{s['line']}  invoke<{s['declared']}>(\"{s['name']}\") "
                f"but Rust returns {cmd['ret']}"
            )

        known = {snake(k) for k in s["keys"]}
        for orig in s["keys"]:
            if snake(orig) not in cmd["params"]:
                problems.append(
                    f"main.ts:{s['line']}  invoke(\"{s['name']}\") passes "
                    f"'{orig}' which is not a parameter (have: "
                    f"{', '.join(cmd['params']) or 'none'})"
                )
        for p in cmd["params"]:
            if p not in known and p not in cmd["optional"]:
                problems.append(
                    f"main.ts:{s['line']}  invoke(\"{s['name']}\") is missing "
                    f"required parameter '{p}'"
                )

    print(f"checked {len(sites)} invoke call site(s) against {len(cmds)} Rust command(s)")
    if problems:
        print("\nIPC contract mismatches:\n")
        for p in problems:
            print(f"  {p}")
        print(
            "\nThese do not fail tsc: the invoke<T> annotation is an assertion, "
            "not a check.\n"
        )
        return 1
    print("all invoke call sites agree with their Rust commands")
    return 0


if __name__ == "__main__":
    sys.exit(main())

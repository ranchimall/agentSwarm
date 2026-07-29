#!/usr/bin/env python3
"""
Plan Fixer (standalone): checks a saved plan.json against the micro-planner
rules, and if it fails, sends the broken plan back to Ollama with the exact
violation and asks it to fix ONLY that problem — then re-checks. Repeats up
to N times until it passes or gives up.

Fully independent — does NOT import micro_planner.py. It carries its own
copy of the same validation rules and Ollama-calling logic, so you can run
it on its own, in any folder, feeding it any plan.json a micro-planner
produced. If you ever change the rules in micro_planner.py, mirror the
change here too (see the "validate()" function below) — they are two
separate copies by design, not shared code.

Usage:
    python3 fix_plan.py --plan-file plan.json
    python3 fix_plan.py --plan-file plan.json --source-file order_processor.py --out plan_fixed.json
"""

import argparse
import json
import re
import sys
import urllib.request
import urllib.error


DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_MODEL = "deepseek-coder-v2:16b"
MAX_RETRIES = 3

REQUIRED_METHOD_FIELDS = [
    "id", "name", "kind", "status", "inputs", "outputs",
    "returns", "side_effects", "depends_on",
]
VALID_STATUSES = {"new", "modified", "existing_unchanged", "removed"}


# ---------------------------------------------------------------------------
# Ollama call (same logic as micro_planner.py, copied so this file is standalone)
# ---------------------------------------------------------------------------

def call_ollama(prompt, model, host=DEFAULT_OLLAMA_HOST, timeout=300):
    url = f"{host.rstrip('/')}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Could not reach Ollama at {url}. Is `ollama serve` running? ({e})"
        ) from e
    return body.get("response", "")


def extract_json(raw_text):
    """Ollama with format=json should already return clean JSON, but this
    strips markdown fences or stray text defensively."""
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in model output")
    return text[start:end + 1]


# ---------------------------------------------------------------------------
# Validation (same rules as micro_planner.py, copied so this file is standalone)
# ---------------------------------------------------------------------------

def validate(plan, source_code):
    """Returns (ok: bool, violation: str or None)."""
    if not isinstance(plan, dict):
        return False, "Top-level output must be a JSON object."

    for key in ("class_name", "members", "methods"):
        if key not in plan:
            return False, f"Top-level key '{key}' is required and missing."

    seen_ids = set()

    for m in plan.get("members", []):
        if "id" not in m or "name" not in m:
            return False, "Every member must have at least 'id' and 'name'."
        if m["id"] in seen_ids:
            return False, f"Duplicate id found: {m['id']}"
        seen_ids.add(m["id"])

    method_names = set()
    for meth in plan.get("methods", []):
        missing = [f for f in REQUIRED_METHOD_FIELDS if f not in meth]
        if missing:
            return False, (
                f"Method '{meth.get('name', '?')}' is missing required "
                f"field(s): {missing}"
            )
        if meth["id"] in seen_ids:
            return False, f"Duplicate id found: {meth['id']}"
        seen_ids.add(meth["id"])

        if meth["status"] not in VALID_STATUSES:
            return False, (
                f"Method '{meth['name']}' has invalid status "
                f"'{meth['status']}'. Must be one of {sorted(VALID_STATUSES)}."
            )

        se = meth["side_effects"]
        if se in ([], "", None):
            return False, (
                f"Method '{meth['name']}' has an empty side_effects field — "
                f"must be [\"none\"] if truly none, never empty."
            )

        method_names.add(meth["name"])

    for meth in plan.get("methods", []):
        for dep in meth.get("depends_on", []):
            if dep not in seen_ids:
                return False, (
                    f"Method '{meth['name']}' has depends_on referencing "
                    f"unknown id '{dep}'."
                )

    if source_code and source_code.strip():
        existing_defs = re.findall(r"def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", source_code)
        for name in existing_defs:
            if name == "__init__":
                continue
            if name not in method_names:
                return False, (
                    f"Method '{name}' exists in current_source_code but is "
                    f"missing from the output. Every existing method must be "
                    f"listed, even as 'existing_unchanged'."
                )

    return True, None


# ---------------------------------------------------------------------------
# Fix logic
# ---------------------------------------------------------------------------

def build_fix_prompt(plan_json_text, violation, source_code):
    source_block = source_code if source_code.strip() else "(no source file provided)"
    return f"""You are fixing a broken JSON plan produced by a micro-planner.

The plan below FAILED validation for this specific reason:
{violation}

Fix ONLY this problem. Do not change anything else that is already
correct — do not rename methods, do not change ids that are not the
problem, do not add or remove methods/members that are not the problem,
do not rewrite fields that already look fine.

Reference source code (for context only, may be empty if class is new):
{source_block}

CURRENT (BROKEN) PLAN:
{plan_json_text}

Output ONLY the corrected JSON. No prose, no markdown fences, no
explanation before or after. The response must start with {{ and end
with }}.
"""


def fix_plan(plan, source_code, model=DEFAULT_MODEL, host=DEFAULT_OLLAMA_HOST,
             max_retries=MAX_RETRIES, verbose=True):
    """Returns (final_plan, was_fixed: bool, violation_history: list[str])."""
    ok, violation = validate(plan, source_code)
    history = []
    if ok:
        return plan, False, history

    current_plan = plan
    for attempt in range(1, max_retries + 1):
        history.append(violation)
        if verbose:
            print(
                f"[fix-plan] attempt {attempt}/{max_retries} — "
                f"asking Ollama to fix: {violation}",
                file=sys.stderr,
            )

        plan_text = json.dumps(current_plan, indent=2)
        prompt = build_fix_prompt(plan_text, violation, source_code)
        raw = call_ollama(prompt, model, host)

        try:
            json_text = extract_json(raw)
            candidate = json.loads(json_text)
        except (ValueError, json.JSONDecodeError) as e:
            violation = f"Fix attempt produced invalid JSON: {e}"
            if verbose:
                print(f"[fix-plan] {violation}", file=sys.stderr)
            continue

        ok, new_violation = validate(candidate, source_code)
        current_plan = candidate
        if ok:
            if verbose:
                print(f"[fix-plan] fixed successfully on attempt {attempt}.", file=sys.stderr)
            return current_plan, True, history

        violation = new_violation

    raise RuntimeError(
        f"fix-plan failed after {max_retries} attempts.\n"
        f"Violation history: {history + [violation]}\n"
        f"Last attempted plan:\n{json.dumps(current_plan, indent=2)}"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Check a plan.json and fix it via Ollama if invalid. Standalone — no dependency on micro_planner.py."
    )
    parser.add_argument("--plan-file", required=True)
    parser.add_argument(
        "--source-file", default=None,
        help="Optional: original source file, so the fixer can also confirm "
             "no existing methods are missing.",
    )
    parser.add_argument(
        "--out", default=None,
        help="Where to write the (possibly fixed) plan. Defaults to "
             "overwriting --plan-file.",
    )
    parser.add_argument("--ollama-model", default=DEFAULT_MODEL)
    parser.add_argument("--ollama-host", default=DEFAULT_OLLAMA_HOST)
    parser.add_argument("--max-retries", type=int, default=MAX_RETRIES)
    args = parser.parse_args()

    try:
        with open(args.plan_file, "r") as f:
            plan = json.load(f)
    except FileNotFoundError:
        print(f"[FAIL] Plan file not found: {args.plan_file}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"[FAIL] Plan file is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    source_code = ""
    if args.source_file:
        with open(args.source_file, "r") as f:
            source_code = f.read()

    out_path = args.out or args.plan_file

    try:
        final_plan, was_fixed, history = fix_plan(
            plan, source_code,
            model=args.ollama_model, host=args.ollama_host,
            max_retries=args.max_retries,
        )
    except RuntimeError as e:
        print(f"[FAIL] Could not fix {args.plan_file}: {e}", file=sys.stderr)
        sys.exit(1)

    if was_fixed:
        print(f"[FIXED] {args.plan_file} had {len(history)} violation(s), now valid.")
        for v in history:
            print(f"         - {v}")
    else:
        print(f"[PASS] {args.plan_file} was already valid — no fix needed.")

    with open(out_path, "w") as f:
        json.dump(final_plan, f, indent=2)
    print(f"Wrote result to {out_path}")


if __name__ == "__main__":
    main()

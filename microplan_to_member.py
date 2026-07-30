#!/usr/bin/env python3
"""
Plan-to-Skeleton Generator (NO LLM — purely deterministic templating).

Takes a micro-planner plan.json and turns it into an actual code skeleton:
the class, its members declared in the constructor, and every method with
its real parameter list and return type — all pulled directly from the
plan's inputs/outputs/returns/side_effects fields. No model call, no
guessing: everything here is already fully specified in the plan, this
script just formats it as code.

Language handling — in priority order:
  1. --language python|javascript, if you pass it explicitly (most reliable)
  2. --source-file, if given: detected from its file extension
  3. Otherwise: guessed from the type words used in the plan itself
     (heuristic, least reliable — the words used often aren't
     language-specific, e.g. "string"/"boolean" get used even for
     Python plans, so this guess can be wrong. Prefer options 1 or 2.)

If --source-file is given AND matches the detected language, this script
will also try to pull the ACTUAL existing code for any method marked
"existing_unchanged" straight out of that file (via Python's ast module
for .py files, or brace-matching for .js files) — so unchanged methods
appear in the skeleton with their real body, not just a stub.

Usage:
    python3 plan_to_skeleton.py --plan-file plan.json --language python
    python3 plan_to_skeleton.py --plan-file plan.json --source-file order_processor.py
    python3 plan_to_skeleton.py --plan-file plan.json --language javascript --out RateLimiter.js
"""

import argparse
import ast
import json
import re
import sys


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

PYTHON_TYPE_MARKERS = {"str", "int", "float", "bool", "dict", "list", "none", "tuple", "bytes"}
JS_TYPE_MARKERS = {"string", "number", "boolean", "object", "array", "void", "undefined", "null"}


def detect_language_from_extension(source_file):
    if source_file.endswith(".py"):
        return "python"
    if source_file.endswith((".js", ".jsx", ".ts", ".tsx")):
        return "javascript"
    return None


def collect_type_words(plan):
    words = []
    for meth in plan.get("methods", []):
        for i in meth.get("inputs", []):
            words.append(str(i.get("type", "")).lower())
        ret = meth.get("returns", {})
        if isinstance(ret, dict):
            words.append(str(ret.get("type", "")).lower())
    for m in plan.get("members", []):
        words.append(str(m.get("type", "")).lower())
    return words


def detect_language_from_plan(plan):
    words = collect_type_words(plan)
    python_hits = sum(1 for w in words if w in PYTHON_TYPE_MARKERS)
    js_hits = sum(1 for w in words if w in JS_TYPE_MARKERS or any(jm in w for jm in JS_TYPE_MARKERS))
    if python_hits > js_hits:
        return "python"
    if js_hits > python_hits:
        return "javascript"
    return None  # genuinely ambiguous


def resolve_language(plan, explicit_language, source_file):
    if explicit_language:
        return explicit_language, "explicit --language flag"

    if source_file:
        lang = detect_language_from_extension(source_file)
        if lang:
            return lang, f"file extension of {source_file}"

    lang = detect_language_from_plan(plan)
    if lang:
        return lang, "guessed from type words in the plan (unreliable — consider passing --language explicitly)"

    return "python", "no signal found anywhere — defaulted to python, verify this is correct"


# ---------------------------------------------------------------------------
# Name casing helpers
# ---------------------------------------------------------------------------

def to_snake_case(name):
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1)
    return s2.lower()


def to_camel_case(name):
    parts = re.split(r"[_\s]+", name)
    parts = [p for p in parts if p]
    if not parts:
        return name
    if len(parts) == 1:
        return parts[0][0].lower() + parts[0][1:] if parts[0] else parts[0]
    return parts[0][0].lower() + parts[0][1:] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------

PY_TYPE_MAP = {
    "string": "str", "str": "str",
    "boolean": "bool", "bool": "bool",
    "number": "float", "float": "float", "int": "int", "integer": "int",
    "void": "None", "none": "None", "null": "None",
    "dict": "dict", "map": "dict", "object": "dict",
    "list": "list", "array": "list",
}

JS_TYPE_MAP = {
    "str": "string", "string": "string",
    "bool": "boolean", "boolean": "boolean",
    "int": "number", "integer": "number", "float": "number", "number": "number",
    "none": "void", "void": "void", "null": "null",
    "dict": "Object", "map": "Object", "object": "Object",
    "list": "Array", "array": "Array",
}


def map_type(raw_type, language):
    if not raw_type:
        return "Any" if language == "python" else "*"
    key = raw_type.strip().lower()
    # strip generic brackets for lookup, e.g. "map<string, int>" -> "map"
    key_base = re.split(r"[<\[]", key)[0].strip()
    table = PY_TYPE_MAP if language == "python" else JS_TYPE_MAP
    return table.get(key_base, raw_type)  # fall back to original if unrecognized (e.g. custom class name)


# ---------------------------------------------------------------------------
# Existing-code extraction (still no LLM — just parsing, for
# "existing_unchanged" methods so their real body is preserved verbatim)
# ---------------------------------------------------------------------------

def extract_python_method_source(source_code, method_name):
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name:
            try:
                return ast.get_source_segment(source_code, node)
            except Exception:
                return None
    return None


def extract_js_method_source(source_code, method_name):
    # Look for "methodName(...) {" (class method shorthand) and brace-match
    # to find the end of the method body. Best-effort, not a full parser.
    pattern = re.compile(rf"\b{re.escape(method_name)}\s*\([^)]*\)\s*\{{")
    m = pattern.search(source_code)
    if not m:
        return None
    start = m.start()
    brace_pos = source_code.index("{", m.start())
    depth = 0
    for idx in range(brace_pos, len(source_code)):
        if source_code[idx] == "{":
            depth += 1
        elif source_code[idx] == "}":
            depth -= 1
            if depth == 0:
                return source_code[start:idx + 1]
    return None  # unbalanced braces, give up


# ---------------------------------------------------------------------------
# Python skeleton generation
# ---------------------------------------------------------------------------

def generate_python_skeleton(plan, source_code=None):
    lines = []
    task_context = plan.get("task_context")
    if task_context:
        lines.append(f'# Big task: {task_context.get("big_task_name", "")}')
        lines.append(f'# Sibling classes in this feature: {", ".join(task_context.get("all_classes", []))}')
        lines.append("")

    gen_from = plan.get("generated_from", {})
    class_name = plan["class_name"]
    lines.append(f"class {class_name}:")
    lines.append(f'    """')
    if gen_from.get("big_goal"):
        lines.append(f'    Goal: {gen_from["big_goal"]}')
    if gen_from.get("subtask_description"):
        lines.append(f'    Subtask: {gen_from["subtask_description"]}')
    lines.append(f'    Auto-generated skeleton from micro-planner output. Fill in TODOs.')
    lines.append(f'    """')
    lines.append("")

    members = plan.get("members", [])
    lines.append("    def __init__(self):")
    if members:
        for m in members:
            py_type = map_type(m.get("type", ""), "python")
            desc = m.get("description", "")
            lines.append(f"        self.{m['name']}: {py_type} = None  # {desc}")
    else:
        lines.append("        pass")
    lines.append("")

    for meth in plan.get("methods", []):
        status = meth.get("status", "new")
        name = meth["name"]

        if status == "removed":
            lines.append(f"    # REMOVED: {name} — delete this method during implementation.")
            lines.append("")
            continue

        existing_body = None
        if status == "existing_unchanged" and source_code:
            existing_body = extract_python_method_source(source_code, name)

        if existing_body:
            # ast.get_source_segment strips leading whitespace only from the
            # FIRST line (the 'def' line) but preserves the original
            # absolute indentation on every line after it. So: re-add the
            # class-level indent to line one only, leave the rest untouched.
            body_lines = existing_body.split("\n")
            lines.append("    " + body_lines[0])
            lines.extend(body_lines[1:])
            lines.append("")
            continue

        params = ["self"]
        for i in meth.get("inputs", []):
            py_type = map_type(i.get("type", ""), "python")
            params.append(f"{i['name']}: {py_type}")
        ret = meth.get("returns", {})
        ret_type = map_type(ret.get("type", ""), "python") if isinstance(ret, dict) else "None"

        lines.append(f"    def {name}({', '.join(params)}) -> {ret_type}:")
        lines.append(f'        """')
        if isinstance(ret, dict) and ret.get("description"):
            lines.append(f"        Returns: {ret['description']}")
        side_effects = meth.get("side_effects", [])
        if side_effects and side_effects != ["none"]:
            lines.append("        Side effects:")
            for se in side_effects:
                lines.append(f"          - {se}")
        if meth.get("notes"):
            lines.append(f"        Notes: {meth['notes']}")
        lines.append(f"        Status: {status}")
        lines.append(f'        """')
        if status == "existing_unchanged":
            lines.append("        # TODO: could not auto-extract original body — paste it here manually.")
        lines.append("        raise NotImplementedError")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JavaScript skeleton generation
# ---------------------------------------------------------------------------

def generate_javascript_skeleton(plan, source_code=None):
    lines = []
    task_context = plan.get("task_context")
    if task_context:
        lines.append(f'// Big task: {task_context.get("big_task_name", "")}')
        lines.append(f'// Sibling classes in this feature: {", ".join(task_context.get("all_classes", []))}')
        lines.append("")

    gen_from = plan.get("generated_from", {})
    class_name = plan["class_name"]
    lines.append("/**")
    if gen_from.get("big_goal"):
        lines.append(f' * Goal: {gen_from["big_goal"]}')
    if gen_from.get("subtask_description"):
        lines.append(f' * Subtask: {gen_from["subtask_description"]}')
    lines.append(" * Auto-generated skeleton from micro-planner output. Fill in TODOs.")
    lines.append(" */")
    lines.append(f"class {class_name} {{")

    members = plan.get("members", [])
    lines.append("  constructor() {")
    if members:
        for m in members:
            desc = m.get("description", "")
            lines.append(f"    this.{m['name']} = null; // {desc}")
    lines.append("  }")
    lines.append("")

    for meth in plan.get("methods", []):
        status = meth.get("status", "new")
        name = to_camel_case(meth["name"])

        if status == "removed":
            lines.append(f"  // REMOVED: {name} — delete this method during implementation.")
            lines.append("")
            continue

        inputs = meth.get("inputs", [])
        param_names = [to_camel_case(i["name"]) for i in inputs]
        ret = meth.get("returns", {})

        lines.append("  /**")
        for i in inputs:
            js_type = map_type(i.get("type", ""), "javascript")
            lines.append(f"   * @param {{{js_type}}} {to_camel_case(i['name'])}")
        if isinstance(ret, dict):
            js_ret_type = map_type(ret.get("type", ""), "javascript")
            lines.append(f"   * @returns {{{js_ret_type}}} {ret.get('description', '')}")
        side_effects = meth.get("side_effects", [])
        if side_effects and side_effects != ["none"]:
            lines.append("   * Side effects:")
            for se in side_effects:
                lines.append(f"   *   - {se}")
        if meth.get("notes"):
            lines.append(f"   * Notes: {meth['notes']}")
        lines.append(f"   * Status: {status}")
        lines.append("   */")

        lines.append(f"  {name}({', '.join(param_names)}) {{")

        existing_body = None
        if status == "existing_unchanged" and source_code:
            existing_body = extract_js_method_source(source_code, meth["name"])

        if existing_body:
            body_lines = existing_body.split("\n")
            # drop the first line (signature) since we already wrote our own,
            # and the final closing brace (we add our own below)
            inner = body_lines[1:-1] if len(body_lines) > 2 else []
            # Dedent to the inner body's own minimum indentation first, then
            # reindent to 4 spaces — avoids double-indenting regardless of
            # how the original file was indented.
            non_empty = [bl for bl in inner if bl.strip()]
            min_indent = min((len(bl) - len(bl.lstrip()) for bl in non_empty), default=0)
            for bl in inner:
                if bl.strip():
                    lines.append("    " + bl[min_indent:])
                else:
                    lines.append("")
        else:
            if status == "existing_unchanged":
                lines.append("    // TODO: could not auto-extract original body — paste it here manually.")
            lines.append('    throw new Error("Not implemented");')

        lines.append("  }")
        lines.append("")

    lines.append("}")
    lines.append("")
    lines.append(f"module.exports = {class_name};")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_skeleton(plan, language, source_code=None):
    if language == "python":
        return generate_python_skeleton(plan, source_code)
    elif language == "javascript":
        return generate_javascript_skeleton(plan, source_code)
    else:
        raise ValueError(f"Unsupported language: {language}")


def main():
    parser = argparse.ArgumentParser(
        description="Convert a micro-planner plan.json into a code skeleton. No LLM used."
    )
    parser.add_argument("--plan-file", required=True)
    parser.add_argument(
        "--language", choices=["python", "javascript"], default=None,
        help="Force the output language. If omitted, detected from "
             "--source-file extension, or guessed from the plan's type words.",
    )
    parser.add_argument(
        "--source-file", default=None,
        help="Optional: original source file. Used for language detection "
             "(via extension) and to pull real bodies for "
             "'existing_unchanged' methods instead of leaving them as stubs.",
    )
    parser.add_argument(
        "--out", default=None,
        help="Output file path. Defaults to <ClassName>.py or .js next to "
             "the plan file.",
    )
    args = parser.parse_args()

    with open(args.plan_file, "r") as f:
        plan = json.load(f)

    language, reason = resolve_language(plan, args.language, args.source_file)
    print(f"[plan-to-skeleton] language: {language} ({reason})", file=sys.stderr)

    source_code = None
    if args.source_file:
        with open(args.source_file, "r") as f:
            source_code = f.read()

    code = generate_skeleton(plan, language, source_code)

    ext = "py" if language == "python" else "js"
    out_path = args.out or f"{plan['class_name']}.{ext}"

    with open(out_path, "w") as f:
        f.write(code)

    print(f"[plan-to-skeleton] wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()

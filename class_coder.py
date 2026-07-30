"""
Class Coder — fills in ONE member or ONE method of a partially-built
class per call.

Sibling to coding_agent.py, not a modification of it. coding_agent.py
stays untouched: this module IMPORTS its generic, code-agnostic pieces
(run_code, strip_code_fence, the parallel best-of-N helpers, the LLM
backend dispatch, human_verify_tests) because those genuinely don't
care whether the code under test is a bare function, a class method,
or a constructor parameter -- coder_service.py already sets the
precedent of importing straight from coding_agent.py rather than
duplicating it.

Two build targets now, sharing one solve loop:

- solve_member(): fills in ONE method's body (unchanged behavior from
  before).
- solve_init_member(): fills in ONE member's real initialization --
  adds a constructor parameter to __init__'s signature and replaces
  that member's `self.x = None` stub with `self.x = x`, leaving every
  other member (still-stub or already-resolved) and every method
  (including NotImplementedError stubs) untouched.

Both call _solve_loop(), the shared best-of-N + fix-loop driver
extracted out of what used to be solve_member()'s body -- the two
targets differ only in what prompts they build and what tests get
generated against, not in how candidates are generated/scored/fixed.

Key design points (apply to both targets):
- The thing being built is narrowly scoped and explicitly labeled for
  the model as the ONLY line(s) allowed to change; everything else
  must come back byte-for-byte.
- Every OTHER method is labeled "safe to call" / "NOT YET IMPLEMENTED"
  exactly as before. Every OTHER member is now labeled "already a real
  constructor parameter" / "still a stub (self.x = None) -- do not
  rely on its value" -- the member-side analogue of the same idea.
- Tests construct the object via the class's constructor, passing
  keyword args for whichever members have real parameters so far
  (resolved this run, in either build order), and DO NOT pass
  still-stub members (they stay None, untouched).
"""

from dataclasses import dataclass

from coding_agent import (
    AgentConfig,
    run_code,
    strip_code_fence,
    call_llm,
    _generate_parallel,
    _evaluate_parallel,
    _best_of,
    human_verify_tests,
)


@dataclass
class ClassAgentConfig(AgentConfig):
    """Identical knobs to AgentConfig (backend, n_candidates, max_iters,
    verify_tests, sandbox limits, ...) — reused via subclassing since
    nothing about the sandbox or backend dispatch changes for
    class-method or class-member solving; only the prompts differ."""
    pass


# ----------------------------------------------------------------------
# Shared context blocks
# ----------------------------------------------------------------------
def _member_context_block(plan: dict) -> str:
    """Full member list, as plain typed-field context. Still used
    everywhere (method prompts, member prompts) so the model always
    knows the complete shape of instance state, not just the one
    member/method it's working on."""
    lines = ["MEMBERS (instance state):"]
    for m in plan.get("members", []):
        lines.append(f"  - self.{m['name']}: {m['type']}  # {m['description']}")
    return "\n".join(lines)


def _sibling_status_block(plan: dict, target_id: str, resolved_method_ids: set) -> str:
    """Per OTHER method: tell the model whether it's real code it can
    rely on, or still a stub it must not assume works. This is what
    lets the DAG's "skip on failure" behavior stay safe — a method
    that was skipped just shows up here as NOT YET IMPLEMENTED."""
    lines = ["OTHER METHODS IN THIS CLASS:"]
    for meth in plan.get("methods", []):
        if meth["id"] == target_id:
            continue
        if meth["id"] in resolved_method_ids:
            note = "implemented and tested this run — safe to call"
        elif meth.get("status") == "existing_unchanged":
            note = "pre-existing code, unmodified — safe to call"
        elif meth.get("status") == "removed":
            note = "marked for removal — do not call"
        else:
            note = "NOT YET IMPLEMENTED (raises NotImplementedError) — do not call or rely on it"
        lines.append(f"  - {meth['name']}(): {note}")
    return "\n".join(lines)


def _member_init_status_block(plan: dict, target_id: str, resolved_member_ids: set) -> str:
    """Per OTHER member: tell the model whether __init__ already takes
    a real constructor parameter for it (safe to assume it has a real
    value if the caller passed one), or whether it's still the
    original `self.x = None` stub (must not be relied on, must not be
    touched)."""
    lines = ["OTHER MEMBERS' __init__ STATUS:"]
    for mem in plan.get("members", []):
        if mem["id"] == target_id:
            continue
        if mem["id"] in resolved_member_ids:
            note = "already a real constructor parameter + assignment — leave as-is"
        else:
            note = "still `self.{} = None` — do not change, do not rely on its value".format(mem["name"])
        lines.append(f"  - self.{mem['name']}: {note}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Prompts — method bodies
# ----------------------------------------------------------------------
def _class_code_gen_prompt(current_code: str, plan: dict, target: dict, resolved_method_ids: set) -> str:
    gen_from = plan.get("generated_from", {})
    return f"""You are implementing ONE method of an existing Python class.
Return the ENTIRE class, unchanged except for the body of `{target['name']}`.

BIG GOAL: {gen_from.get('big_goal', '')}
SUBTASK: {gen_from.get('subtask_description', '')}

CURRENT FULL CLASS CODE (implement only `{target['name']}`; every other
line — including __init__, and other methods, even ones still stubbed —
must be reproduced verbatim):
{current_code}

{_member_context_block(plan)}

{_sibling_status_block(plan, target['id'], resolved_method_ids)}

METHOD TO IMPLEMENT: {target['name']}
  inputs: {target.get('inputs', [])}
  returns: {target.get('returns', {})}
  side_effects: {target.get('side_effects', [])}
  notes: {target.get('notes', '')}

Rules:
- Only return the full class source code — no explanation, no markdown fences
- Do not rename or change the signature of `{target['name']}` or any other method
- Do not modify __init__ or any method other than `{target['name']}`"""


def _class_test_gen_prompt(plan: dict, target: dict, resolved_method_ids: set) -> str:
    return f"""Write Python pytest tests for ONE method of a class.

CLASS: {plan['class_name']}
{_member_context_block(plan)}

{_sibling_status_block(plan, target['id'], resolved_method_ids)}

METHOD UNDER TEST: {target['name']}
  inputs: {target.get('inputs', [])}
  returns: {target.get('returns', {})}
  notes: {target.get('notes', '')}

Rules:
- Write separate functions named test_case_1, test_case_2, ... (do NOT bundle into one test)
- Construct the object with `{plan['class_name']}()`, passing keyword args for any
  members that already have real constructor parameters (check the class code you're
  testing against to see which); for members still stubbed as None, set them directly
  as attributes before calling `{target['name']}` (e.g. `obj.threshold = 3`)
- Only call `{target['name']}` and methods explicitly marked "safe to call" above —
  never call a method marked NOT YET IMPLEMENTED
- Use assert statements, cover edge cases
- Do NOT define `{plan['class_name']}` or `{target['name']}` yourself — it will be
  imported via `from solution import *`; redefining it here silently shadows the
  real candidate and makes every test meaningless
- No explanations, no markdown fences, no code outside the test_case_N functions"""


def _class_fix_prompt(current_code: str, plan: dict, target: dict, tests: str,
                       error: str, resolved_method_ids: set) -> str:
    return f"""The following class is failing tests for method `{target['name']}`.

{_member_context_block(plan)}
{_sibling_status_block(plan, target['id'], resolved_method_ids)}

CURRENT FULL CLASS CODE:
{current_code}

TESTS:
{tests}

ERROR:
{error}

Fix ONLY `{target['name']}`. Return the entire class, every other line
(including __init__ and other methods) reproduced verbatim.

Rules:
- Only return corrected code
- Do not rename or change any signature
- Do not modify tests"""


# ----------------------------------------------------------------------
# Prompts — member initialization (__init__)
# ----------------------------------------------------------------------
def _member_init_code_gen_prompt(current_code: str, plan: dict, target: dict,
                                  resolved_member_ids: set) -> str:
    gen_from = plan.get("generated_from", {})
    return f"""You are giving ONE member of an existing Python class a real
constructor parameter, inside `__init__`. Return the ENTIRE class,
unchanged except for `__init__`'s signature and body, and even there,
touch only the parts that concern `{target['name']}`.

BIG GOAL: {gen_from.get('big_goal', '')}
SUBTASK: {gen_from.get('subtask_description', '')}

CURRENT FULL CLASS CODE (every method body, including NotImplementedError
stubs, and every other member's line in __init__, must be reproduced
verbatim):
{current_code}

{_member_context_block(plan)}

{_member_init_status_block(plan, target['id'], resolved_member_ids)}

MEMBER TO INITIALIZE: self.{target['name']}
  type: {target.get('type', '')}
  description: {target.get('description', '')}

Do exactly this, and nothing else:
1. In `__init__`'s signature, add one new parameter for this member.
   - If the type is anything OTHER than list/dict/set (e.g. int, str,
     float, bool): give it a sensible literal default given its
     type/description, e.g. `{target['name']}: {target.get('type', '')} = <default>`.
   - If the type IS list/dict/set (or a generic alias of one, e.g.
     `list`, `List[int]`, `dict`, `Dict[str, int]`, `set`): the
     parameter's default MUST be `None`, never a mutable literal
     (`[]`, `{{}}`, `set()`). A mutable default argument is evaluated
     ONCE at function-definition time and then SHARED by every
     instance that doesn't pass its own value -- one RateLimiter's
     request_log would silently leak into another's. Write
     `{target['name']}: {target.get('type', '')} = None` in the signature.
2. In `__init__`'s body, replace the line `self.{target['name']} = None`:
   - Non-mutable type: with `self.{target['name']} = {target['name']}`.
   - list/dict/set type: with
     `self.{target['name']} = {target['name']} if {target['name']} is not None else <empty literal>`
     where `<empty literal>` is `[]`, `{{}}`, or `set()` as appropriate --
     this constructs a FRESH empty container per instance, at call time,
     instead of sharing one across instances.
3. Leave every other member's line untouched -- resolved ones keep their
   real assignment, still-stub ones keep `self.x = None` exactly as-is.
4. Leave every method body untouched, including NotImplementedError stubs.

Rules:
- Only return the full class source code — no explanation, no markdown fences
- Do not rename `__init__`'s existing parameters or remove any of them
- Never use a mutable literal (`[]`, `{{}}`, `set()`) as a parameter default
- Do not touch any member other than `{target['name']}`
- Do not touch any method"""


def _member_init_test_gen_prompt(plan: dict, target: dict, resolved_member_ids: set) -> str:
    resolved_names = [m["name"] for m in plan.get("members", []) if m["id"] in resolved_member_ids]
    resolved_names_with_target = resolved_names + [target["name"]]
    return f"""Write Python pytest tests that verify constructing an instance
of a class with real constructor arguments actually sets the right
attributes.

CLASS: {plan['class_name']}
{_member_context_block(plan)}

{_member_init_status_block(plan, target['id'], resolved_member_ids)}

MEMBER UNDER TEST: self.{target['name']}
  type: {target.get('type', '')}
  description: {target.get('description', '')}

Rules:
- Write separate functions named test_case_1, test_case_2, ... (do NOT bundle into one test)
- Construct the object passing keyword arguments ONLY for these members
  (all of them have, or will have, real constructor parameters):
  {resolved_names_with_target}
- Do NOT pass keyword arguments for any other member -- they're still
  `None` stubs and passing them would fail
- Assert that each attribute you passed in was actually stored
  (e.g. `obj.{target['name']} == <value you passed>`)
- Do NOT call any method on the object -- this is only testing construction
- Do NOT define `{plan['class_name']}` yourself — it will be imported via
  `from solution import *`; redefining it here silently shadows the real
  candidate and makes every test meaningless
- No explanations, no markdown fences, no code outside the test_case_N functions"""


def _member_init_fix_prompt(current_code: str, plan: dict, target: dict, tests: str,
                             error: str, resolved_member_ids: set) -> str:
    return f"""The following class is failing tests for the constructor
parameter of member `self.{target['name']}`.

{_member_context_block(plan)}
{_member_init_status_block(plan, target['id'], resolved_member_ids)}

CURRENT FULL CLASS CODE:
{current_code}

TESTS:
{tests}

ERROR:
{error}

Fix ONLY the part of `__init__` concerning `self.{target['name']}`. Return
the entire class, every other line (including every method body and every
other member's line in __init__) reproduced verbatim.

Rules:
- Only return corrected code
- Do not rename or remove any existing __init__ parameter
- If `self.{target['name']}`'s type is list/dict/set, its parameter default
  MUST be `None` (never a mutable literal like `[]`/`{{}}`/`set()`), with the
  body doing `self.{target['name']} = {target['name']} if {target['name']} is not None else <empty literal>`
  -- a mutable default is shared across every instance and is a bug, not a style choice
- Do not modify tests"""


# ----------------------------------------------------------------------
# Shared solve loop
# ----------------------------------------------------------------------
def _solve_loop(initial_code_gen_prompt: str, fix_prompt_fn, tests: str,
                 label: str, cfg: ClassAgentConfig) -> dict:
    """Shared best-of-N + fix-loop driver, used by both solve_member()
    (method bodies) and solve_init_member() (constructor params) -- the
    two differ only in what prompts they build; the generate/evaluate/
    fix loop itself is identical, so it lives here once.

    fix_prompt_fn: callable(code: str, tests: str, error: str) -> str,
    a closure supplied by the caller that already has plan/target/
    resolved_ids baked in -- keeps this function ignorant of whether
    it's fixing a method body or a constructor line.
    """
    print(f"\n=== Round 0: generating {cfg.n_candidates} candidate(s) for {label} ===")
    candidates = _generate_parallel(initial_code_gen_prompt, cfg)
    scored = _evaluate_parallel(candidates, tests, cfg)
    for i, (_, r) in enumerate(scored):
        print(f"  candidate {i}: {r['total'] - r['failed_count']}/{r['total']} passing")

    code, result = _best_of(scored)
    best = {"code": code, "score": result["score"], "result": result}
    history = [{"iteration": 0, "candidates_evaluated": cfg.n_candidates, "best_score": result["score"]}]

    tests_verified = cfg.verify_tests

    for i in range(1, cfg.max_iters):
        if best["result"]["passed"]:
            print(f"All tests passed for {label}.")
            break

        if (not tests_verified and cfg.auto_verify_after_rounds > 0
                and i == cfg.auto_verify_after_rounds):
            print(f"\n=== {label}: still failing after {i} round(s) (best so far "
                  f"{best['score']:.0%}). Pausing for human test verification. ===")
            tests = human_verify_tests(tests, label)
            tests_verified = True
            result = run_code(best["code"], tests, cfg)
            best = {"code": best["code"], "score": result["score"], "result": result}
            history.append({"iteration": i, "note": "auto-triggered human test verification",
                             "best_score": result["score"]})
            if best["result"]["passed"]:
                break

        print(f"\n=== Round {i}: generating {cfg.n_candidates} fix candidate(s) for {label} ===")
        fix_prompt = fix_prompt_fn(best["code"], tests, best["result"]["stderr"])
        candidates = _generate_parallel(fix_prompt, cfg)
        scored = _evaluate_parallel(candidates, tests, cfg)
        round_best_code, round_best_result = _best_of(scored)
        history.append({"iteration": i, "candidates_evaluated": cfg.n_candidates,
                         "best_score": round_best_result["score"]})
        if round_best_result["score"] > best["score"]:
            best = {"code": round_best_code, "score": round_best_result["score"], "result": round_best_result}

    return {
        "final_code": strip_code_fence(best["code"]),
        "score": best["score"],
        "passed": best["result"]["passed"],
        "history": history,
    }


# ----------------------------------------------------------------------
# Public entry points
# ----------------------------------------------------------------------
def solve_member(current_code: str, plan: dict, target: dict, resolved_method_ids: set,
                  cfg: ClassAgentConfig = None) -> dict:
    """Fills in ONE method (`target`, a dict from plan['methods']) of
    the class currently represented by `current_code`."""
    if cfg is None:
        cfg = ClassAgentConfig()

    label = f"{plan['class_name']}.{target['name']}"

    tests = call_llm(_class_test_gen_prompt(plan, target, resolved_method_ids), cfg)
    tests = strip_code_fence(tests)
    if cfg.verify_tests:
        tests = human_verify_tests(tests, f"{label}: {target.get('notes', '')}")

    gen_prompt = _class_code_gen_prompt(current_code, plan, target, resolved_method_ids)

    def fix_prompt_fn(code, tests_, error):
        return _class_fix_prompt(code, plan, target, tests_, error, resolved_method_ids)

    return _solve_loop(gen_prompt, fix_prompt_fn, tests, label, cfg)


def solve_init_member(current_code: str, plan: dict, target: dict, resolved_member_ids: set,
                       cfg: ClassAgentConfig = None) -> dict:
    """Fills in ONE member (`target`, a dict from plan['members']) of
    the class currently represented by `current_code` -- gives it a
    real constructor parameter + assignment in `__init__`, leaving
    every other member and every method untouched. Same best-of-N +
    fix-loop shape as solve_member(), via the shared _solve_loop()."""
    if cfg is None:
        cfg = ClassAgentConfig()

    label = f"{plan['class_name']}.__init__:{target['name']}"

    tests = call_llm(_member_init_test_gen_prompt(plan, target, resolved_member_ids), cfg)
    tests = strip_code_fence(tests)
    if cfg.verify_tests:
        tests = human_verify_tests(tests, f"{label}: {target.get('description', '')}")

    gen_prompt = _member_init_code_gen_prompt(current_code, plan, target, resolved_member_ids)

    def fix_prompt_fn(code, tests_, error):
        return _member_init_fix_prompt(code, plan, target, tests_, error, resolved_member_ids)

    return _solve_loop(gen_prompt, fix_prompt_fn, tests, label, cfg)

"""
Class Coordinator — drives the "one member/method at a time" build loop.

Input: the class skeleton produced by microplan_to_member.py, plus the
plan.json it came from (produced by micro_planner.py). Orders the
class's buildable MEMBERS and METHODS together with class_dag.ClassDAG
(one unified topo order now, members before the methods that need
them), then dispatches each node ONE AT A TIME to the Coder Service
over HTTP -- members to POST /solve_init_member, methods to POST
/solve_member -- splicing each result back into a running copy of the
class before moving to the next node.

Previously only methods were dispatched; members were declared once by
the skeleton and never touched again, leaving __init__ full of
`self.x = None` stubs forever. Now every member gets its own
Coder Service round-trip too, same skip-on-failure safety net as
methods: if a member's constructor param can't be made to pass tests,
its `self.x = None` stub is left in place, it's recorded as failed,
and anything depending on it downstream is told explicitly (via
resolved_member_ids / resolved_method_ids) that it's NOT resolved.

Requires the Coder Service to already be running (see coder_service.py
-- `uvicorn coder_service:app --port 8001`, or run_all.py).

Standalone CLI:
    python3 class_coordinator.py --plan-file plan.json --skeleton-file RateLimiter.py
"""

import argparse
import json
import sys

import requests

from class_dag import ClassDAG, CycleError, UnknownDependencyError


CODER_SERVICE_URL = "http://localhost:8001"


def _check_service_up(url: str) -> bool:
    try:
        return requests.get(f"{url}/health", timeout=3).status_code == 200
    except requests.exceptions.ConnectionError:
        return False


def build_class(
    plan: dict,
    skeleton_code: str,
    backend: str = "mock",
    ollama_model: str = "llama3",
    ollama_url: str = "http://localhost:11434",
    verify_tests: bool = False,
    coder_url: str = CODER_SERVICE_URL,
) -> dict:
    dag = ClassDAG(plan)
    order = dag.topo_order()

    current_code = skeleton_code
    resolved_ids = set()          # union of resolved member ids + method ids
    failed_ids = set()
    per_method_results = []

    n_members = sum(1 for n in order if n.kind == "member")
    n_methods = sum(1 for n in order if n.kind == "method")
    print(f"=== Class Coordinator: building {plan['class_name']} "
          f"({n_members} member(s), {n_methods} method(s) to implement) ===")
    if order:
        print("Build order: " + " -> ".join(f"{n.name}[{n.kind}]" for n in order))
    print()

    for node in order:
        kind_label = "member init" if node.kind == "member" else "method"
        print(f"--- Dispatching '{node.name}' ({kind_label}) to Coder Service ---")

        unresolved_deps = [
            d for d in node.depends_on
            if d not in resolved_ids and d in dag.nodes
            and (
                (dag.nodes[d].kind == "method" and dag.nodes[d].status in ("new", "modified"))
                or dag.nodes[d].kind == "member"
            )
        ]
        if unresolved_deps:
            dep_names = [dag.nodes[d].name for d in unresolved_deps]
            print(f"  note: dependenc{'y' if len(dep_names) == 1 else 'ies'} {dep_names} "
                  f"were skipped earlier -- '{node.name}' will be told not to rely on "
                  f"{'it' if len(dep_names) == 1 else 'them'}.")

        resolved_member_ids = [i for i in resolved_ids if dag.nodes[i].kind == "member"]
        resolved_method_ids = [i for i in resolved_ids if dag.nodes[i].kind == "method"]

        if node.kind == "member":
            payload = {
                "task_id": f"{plan['class_id']}-{node.id}",
                "plan": plan,
                "current_code": current_code,
                "target_member_id": node.id,
                "resolved_member_ids": resolved_member_ids,
                "backend": backend,
                "ollama_model": ollama_model,
                "ollama_url": ollama_url,
                "verify_tests": verify_tests,
            }
            endpoint = "/solve_init_member"
        else:
            payload = {
                "task_id": f"{plan['class_id']}-{node.id}",
                "plan": plan,
                "current_code": current_code,
                "target_method_id": node.id,
                "resolved_method_ids": resolved_method_ids,
                "backend": backend,
                "ollama_model": ollama_model,
                "ollama_url": ollama_url,
                "verify_tests": verify_tests,
            }
            endpoint = "/solve_member"

        try:
            resp = requests.post(f"{coder_url}{endpoint}", json=payload, timeout=None)
            resp.raise_for_status()
            result = resp.json()
        except requests.exceptions.ConnectionError:
            print(f"  Could not reach Coder Service at {coder_url}. Skipping '{node.name}'.\n")
            failed_ids.add(node.id)
            per_method_results.append({"method": node.name, "kind": node.kind, "status": "unreachable"})
            continue
        except requests.exceptions.HTTPError as e:
            print(f"  Coder Service returned an error for '{node.name}': {e}. Skipping.\n")
            failed_ids.add(node.id)
            per_method_results.append({"method": node.name, "kind": node.kind,
                                        "status": "http_error", "detail": str(e)})
            continue

        if result["status"] == "error":
            print(f"  Coder failed on '{node.name}': {result.get('error')}. "
                  f"Skipping, leaving previous stub in place.\n")
            failed_ids.add(node.id)
            per_method_results.append({"method": node.name, "kind": node.kind,
                                        "status": "error", "detail": result.get("error")})
            continue

        if not result.get("passed"):
            score = result.get("score") or 0.0
            print(f"  Coder did not pass all tests for '{node.name}' (score={score:.0%}). "
                  f"Skipping, leaving previous stub in place.\n")
            failed_ids.add(node.id)
            per_method_results.append({"method": node.name, "kind": node.kind,
                                        "status": "skipped", "score": score})
            continue

        current_code = result["updated_code"]
        resolved_ids.add(node.id)
        print(f"  '{node.name}' solved (score={result['score']:.0%}). Spliced into class.\n")
        per_method_results.append({"method": node.name, "kind": node.kind,
                                    "status": "done", "score": result["score"]})

    print("=== Build summary ===")
    for r in per_method_results:
        score_str = f" ({r['score']:.0%})" if r.get("score") is not None else ""
        print(f"  {r['method']} [{r['kind']}]: {r['status']}{score_str}")
    if failed_ids:
        print(f"\n{len(failed_ids)} member(s)/method(s) left unresolved -- see summary above.")
    print()

    return {
        "final_code": current_code,
        "resolved": [dag.nodes[i].name for i in resolved_ids],
        "failed": [dag.nodes[i].name for i in failed_ids],
        "per_method_results": per_method_results,
    }


def build_arg_parser():
    p = argparse.ArgumentParser(
        description="Class Coordinator: build a class member-by-member and method-by-method via the Coder Service"
    )
    p.add_argument("--plan-file", required=True, help="plan.json from micro_planner.py")
    p.add_argument("--skeleton-file", required=True, help="skeleton .py from microplan_to_member.py")
    p.add_argument("--backend", choices=["mock", "ollama", "openai", "anthropic"], default="mock")
    p.add_argument("--ollama-model", default="llama3")
    p.add_argument("--ollama-url", default="http://localhost:11434")
    p.add_argument("--verify-tests", action="store_true")
    p.add_argument("--coder-url", default=CODER_SERVICE_URL)
    p.add_argument("--out", default=None, help="Write the final assembled class here (in addition to stdout)")
    return p


if __name__ == "__main__":
    args = build_arg_parser().parse_args()

    with open(args.plan_file, "r", encoding="utf-8") as f:
        plan = json.load(f)
    with open(args.skeleton_file, "r", encoding="utf-8") as f:
        skeleton_code = f.read()

    if not _check_service_up(args.coder_url):
        print(f"Warning: could not reach the Coder Service at {args.coder_url}.")
        print("Start it first: uvicorn coder_service:app --port 8001\n")

    try:
        result = build_class(
            plan, skeleton_code,
            backend=args.backend, ollama_model=args.ollama_model, ollama_url=args.ollama_url,
            verify_tests=args.verify_tests, coder_url=args.coder_url,
        )
    except (CycleError, UnknownDependencyError) as e:
        print(f"\nError building DAG: {e}")
        sys.exit(1)

    print("=== FINAL ASSEMBLED CLASS ===\n")
    print(result["final_code"])

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(result["final_code"])
        print(f"\nWrote final class to {args.out}")

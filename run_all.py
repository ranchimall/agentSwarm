"""
run_all.py — starts the Coder Service and Plan Controller in the
background, then either:

  (a) hands control to the interactive Coordinator Console (default,
      unchanged behavior), or
  (b) runs a ONE-SHOT class build via class_coordinator.build_class()
      and exits, if --plan-file and --skeleton-file are both given.

Ctrl-C (or 'exit'/'quit' at the task> prompt, in console mode) stops
things and shuts down the Coder Service + Plan Controller subprocesses
either way.

Usage -- interactive console (unchanged):
    python3 run_all.py --backend ollama --ollama-model deepseek-coder-v2:16b
    python3 run_all.py --backend ollama --ollama-model deepseek-coder-v2:16b --verify-tests

Usage -- one-shot class build (new):
    python3 run_all.py --plan-file plan.json --skeleton-file RateLimiter.py \\
        --backend ollama --ollama-model deepseek-coder-v2:16b
    python3 run_all.py --plan-file plan.json --skeleton-file RateLimiter.py \\
        --backend mock --out final_class.py
"""

import json
import subprocess
import sys
import time

import requests

import coordinator_console
import class_coordinator
from class_dag import CycleError, UnknownDependencyError


CODER_PORT = 8001
CODER_URL = f"http://localhost:{CODER_PORT}"

PLAN_PORT = 8002
PLAN_URL = f"http://localhost:{PLAN_PORT}"


def wait_for_service(url: str, timeout_sec: int = 20) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            if requests.get(f"{url}/health", timeout=1).status_code == 200:
                return True
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(0.5)
    return False


def build_arg_parser():
    """Same flags as coordinator_console.build_arg_parser() (backend,
    ollama settings, --verify-tests, --coder-url, --plan-url), plus
    three new optional ones. Passing --plan-file (together with
    --skeleton-file) is what switches run_all.py from "start the
    interactive console" to "run one class build and exit" -- nothing
    about how the two services get started or torn down changes
    either way."""
    p = coordinator_console.build_arg_parser()
    p.add_argument("--plan-file", default=None,
                    help="plan.json from micro_planner.py. If given together with "
                         "--skeleton-file, run_all.py builds that one class via "
                         "class_coordinator.build_class() and exits, instead of "
                         "starting the interactive console.")
    p.add_argument("--skeleton-file", default=None,
                    help="skeleton .py from microplan_to_member.py, paired with --plan-file.")
    p.add_argument("--out", default=None,
                    help="Class-build mode only: also write the final assembled class here.")
    return p


def _run_class_build(args) -> int:
    print("[run_all] --plan-file given -- running one-shot class build "
          "instead of the interactive console.\n")
    with open(args.plan_file, "r", encoding="utf-8") as f:
        plan = json.load(f)
    with open(args.skeleton_file, "r", encoding="utf-8") as f:
        skeleton_code = f.read()

    try:
        result = class_coordinator.build_class(
            plan, skeleton_code,
            backend=args.backend, ollama_model=args.ollama_model,
            ollama_url=args.ollama_url, verify_tests=args.verify_tests,
            coder_url=args.coder_url,
        )
    except (CycleError, UnknownDependencyError) as e:
        print(f"\n[run_all] Error building DAG: {e}")
        return 1

    print("=== FINAL ASSEMBLED CLASS ===\n")
    print(result["final_code"])

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(result["final_code"])
        print(f"\n[run_all] wrote final class to {args.out}")

    return 0


def main():
    args = build_arg_parser().parse_args()

    if bool(args.plan_file) != bool(args.skeleton_file):
        print("[run_all] --plan-file and --skeleton-file must be given together.")
        sys.exit(1)

    print(f"[run_all] starting Coder Service on port {CODER_PORT} ...")
    coder_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "coder_service:app",
         "--port", str(CODER_PORT), "--log-level", "warning"],
    )

    print(f"[run_all] starting Plan Controller on port {PLAN_PORT} ...")
    plan_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "plan_controller:app",
         "--port", str(PLAN_PORT), "--log-level", "warning"],
    )

    exit_code = 0
    try:
        if not wait_for_service(CODER_URL):
            print(f"[run_all] Coder Service did not come up within the timeout. "
                  f"Check for errors above.")
            coder_proc.terminate()
            plan_proc.terminate()
            sys.exit(1)

        if not wait_for_service(PLAN_URL):
            print(f"[run_all] Plan Controller did not come up within the timeout. "
                  f"Check for errors above.")
            coder_proc.terminate()
            plan_proc.terminate()
            sys.exit(1)

        print(f"[run_all] Coder Service is up at {CODER_URL}")
        print(f"[run_all] Plan Controller is up at {PLAN_URL}\n")

        if args.plan_file:
            exit_code = _run_class_build(args)
        else:
            coordinator_console.run_console(
                backend=args.backend,
                ollama_model=args.ollama_model,
                ollama_url=args.ollama_url,
                verify_tests=args.verify_tests,
                coder_url=args.coder_url,
                plan_url=args.plan_url,
            )
    finally:
        print("[run_all] shutting down Coder Service and Plan Controller ...")
        coder_proc.terminate()
        plan_proc.terminate()
        for proc in (coder_proc, plan_proc):
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()

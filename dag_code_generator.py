### File 2: dag_code_generator.py (Generator)

import inspect
import textwrap
from pathlib import Path
from typing import Dict, Any, List


class DAGCodeGenerator:
    """
    Generates a standalone, executable Python script (xyz.py) from the DAG.
    """

    def __init__(self, classes: Dict[str, Any], dag: Dict[str, List[str]]):
        self.classes = classes
        self.dag = dag

    def generate(self, output_path: str = "xyz.py", run_first: bool = False) -> str:
        if run_first:
            from dag_integrator import DAGIntegrator
            print("Running DAG to validate...")
            integrator = DAGIntegrator(self.classes, self.dag)
            _, execution_order = integrator.execute()
        else:
            execution_order = self._topological_order()

        code = self._generate_header()
        code += self._generate_class_sources()
        code += self._generate_main_function(execution_order)
        code += self._generate_entry_point()

        Path(output_path).write_text(code, encoding="utf-8")
        print(f"✅ Generated: {output_path}")

        return code

    def _topological_order(self) -> List[str]:
        from collections import defaultdict, deque
        graph = defaultdict(list)
        indegree = {node: 0 for node in self.dag}

        for node, deps in self.dag.items():
            for dep in deps:
                if dep not in self.dag:
                    raise KeyError(f"Node '{node}' depends on unknown node '{dep}'")
                graph[dep].append(node)
                indegree[node] += 1

        queue = deque(n for n in indegree if indegree[n] == 0)
        order = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for nxt in graph[node]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)

        if len(order) != len(self.dag):
            raise ValueError("Cycle detected in DAG")
        return order

    def _generate_header(self) -> str:
        return '''#!/usr/bin/env python3
"""
Auto-generated standalone DAG execution script.
"""

import sys

'''

    def _generate_class_sources(self) -> str:
        lines = ["# === Node Classes ===\n"]
        seen = set()

        for name, entry in self.classes.items():
            cls = entry if inspect.isclass(entry) else entry.__class__
            try:
                source = textwrap.dedent(inspect.getsource(cls))
                if source not in seen:
                    lines.append(source + "\n")
                    seen.add(source)
            except Exception:
                lines.append(f"# Could not extract source for {name}\n")

            if cls.__name__ != name:
                lines.append(f"{name} = {cls.__name__}\n\n")

        return "\n".join(lines)

    def _generate_main_function(self, order: List[str]) -> str:
        body = ["def main():", "    outputs = {}", ""]

        for node in order:
            deps = self.dag.get(node, [])
            args_str = ", ".join(f"outputs['{d}']" for d in deps)
            body.append(f"    # {node}")
            body.append("    try:")
            body.append(f"        runnable = {node}()")
            body.append(f"        outputs['{node}'] = runnable.run({args_str})")
            body.append("    except Exception as e:")
            body.append(f'        raise RuntimeError(f"Node \'{node}\' failed: {{e}}") from e')
            body.append("")

        body.append("    print('✅ DAG executed successfully!')")
        body.append("    return outputs")

        return "\n# === Main Execution ===\n\n" + "\n".join(body) + "\n"

    def _generate_entry_point(self) -> str:
        return '''
if __name__ == "__main__":
    try:
        results = main()
        print("\\nOutputs:")
        for k, v in results.items():
            print(f"  {k}: {type(v).__name__}")
    except Exception as e:
        print(f"❌ Failed: {e}", file=sys.stderr)
        sys.exit(1)
'''

### Usage (runs when you execute this file)
if __name__ == "__main__":
    from example_nodes import get_example_dag
    classes, dag = get_example_dag()
    generator = DAGCodeGenerator(classes, dag)
    generator.generate("xyz.py", run_first=True)
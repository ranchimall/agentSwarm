### File 1: dag_integrator.py (Executor)

import inspect
from collections import defaultdict, deque
from typing import Dict, List, Any, Tuple


class DAGIntegrator:
    def init(self, classes: Dict[str, Any], dag: Dict[str, List[str]]):
        self.classes = classes
        self.dag = dag
        self.outputs = {}
        self._validate()

    def _validate(self):
        for node, deps in self.dag.items():
            if node not in self.classes:
                raise KeyError(f"No class/instance registered for node '{node}'")
            for dep in deps:
                if dep not in self.dag:
                    raise KeyError(f"Node '{node}' depends on unknown node '{dep}'")

    def _get_runnable(self, node: str):
        entry = self.classes[node]
        if inspect.isclass(entry):
            return entry()
        return entry

    def execute(self) -> Tuple[Dict[str, Any], List[str]]:
        self.outputs = {}

        graph = defaultdict(list)
        indegree = {node: 0 for node in self.dag}

        for node, deps in self.dag.items():
            for dep in deps:
                graph[dep].append(node)
                indegree[node] += 1

        queue = deque([node for node in indegree if indegree[node] == 0])
        execution_order = []

        while queue:
            node = queue.popleft()
            execution_order.append(node)

            args = [self.outputs[d] for d in self.dag[node]]

            runnable = self._get_runnable(node)
            try:
                result = runnable.run(*args)
            except Exception as e:
                raise RuntimeError(f"Node '{node}' failed: {e}") from e

            self.outputs[node] = result

            for nxt in graph[node]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)

        if len(execution_order) != len(self.dag):
            missing = set(self.dag) - set(execution_order)
            raise ValueError(f"Cycle or unreachable nodes: {missing}")

        return self.outputs, execution_order

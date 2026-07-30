import inspect
from collections import defaultdict, deque


class DAGIntegrator:
    def __init__(self, classes, dag):
        """
        classes : {
            "ConfigLoader": ConfigLoader,       # class (preferred, lazy instantiation)
            "DatabaseConnector": DatabaseConnector(),  # or an already-built instance
            ...
        }
        dag : {
            "ConfigLoader": [],
            "DatabaseConnector": ["ConfigLoader"],
            ...
        }

        Passing classes (not instances) is preferred: it guarantees a node's
        constructor doesn't run until its dependencies have actually finished,
        which matters if __init__ has side effects (opening connections, etc).
        Instances are still accepted for backward compatibility.
        """
        self.classes = classes
        self.dag = dag
        self.outputs = {}
        self._validate()

    def _validate(self):
        """Catch config errors early instead of failing silently at runtime."""
        for node, deps in self.dag.items():
            if node not in self.classes:
                raise KeyError(f"No class/instance registered for node '{node}'")
            for dep in deps:
                if dep not in self.dag:
                    raise KeyError(
                        f"Node '{node}' depends on unknown node '{dep}' "
                        f"(not a key in the dag)"
                    )

    def _get_runnable(self, node):
        """Return an object with a .run() method, instantiating lazily if needed."""
        entry = self.classes[node]
        if inspect.isclass(entry):
            return entry()
        return entry

    def execute(self):
        # Reset in case this instance is reused
        self.outputs = {}

        # Build graph
        graph = defaultdict(list)
        indegree = defaultdict(int)
        for node in self.dag:
            indegree[node] = 0
        for node, deps in self.dag.items():
            for dep in deps:
                graph[dep].append(node)
                indegree[node] += 1

        # Nodes with no dependencies
        queue = deque()
        for node in indegree:
            if indegree[node] == 0:
                queue.append(node)

        execution_order = []
        while queue:
            node = queue.popleft()
            execution_order.append(node)

            # Collect dependency outputs
            dependencies = self.dag[node]
            args = [self.outputs[d] for d in dependencies]

            runnable = self._get_runnable(node)
            try:
                result = runnable.run(*args)
            except Exception as e:
                raise RuntimeError(f"Node '{node}' failed: {e}") from e

            self.outputs[node] = result

            # Reduce indegree
            for nxt in graph[node]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)

        # Cycle / unreachable-node detection
        if len(execution_order) != len(self.dag):
            missing = set(self.dag) - set(execution_order)
            raise ValueError(
                f"Cycle detected (or unreachable nodes) involving: {missing}"
            )

        return self.outputs, execution_order

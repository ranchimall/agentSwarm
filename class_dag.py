"""
Class DAG — orders a micro-planner class plan's MEMBERS *and* METHODS
for build, as one unified sequence.

Previously this module only scheduled methods; members were pure
context text, never built. Now members are real nodes too: each one
represents "give this member a real constructor parameter + assignment
in __init__", same way a method node represents "give this method a
real body". Members have no dependencies by default (they're just
typed fields, nothing to build on top of), so in a normal topo sort
they always land before any method whose depends_on lists them --
methods still declare those member ids in depends_on exactly as
before, that's what keeps the ordering correct. This module doesn't
special-case "members first"; it falls out of the same Kahn's-
algorithm pass that ordered methods before, because member nodes have
indegree 0 and methods that need them have a real edge into them.

Nodes = plan["members"] (always buildable -- every member starts as
an untouched `self.x = None` stub and needs a real constructor
parameter) plus plan["methods"] with status "new" or "modified"
(same buildability rule as before -- "existing_unchanged" methods
already have real code, "removed" ones aren't being built at all;
neither enters the DAG or blocks anything).

Edges = depends_on entries that point at another buildable node,
whether that node is a member or a method. A method depending on
mem_0001 gets a real edge from mem_0001 -> that method now (member
nodes are buildable), instead of being filtered out as purely
informational like before.

Standalone module, deliberately not importing from planner.py — same
pattern the rest of the project uses to keep each layer independent.
"""

from dataclasses import dataclass, field


class CycleError(Exception):
    pass


class UnknownDependencyError(Exception):
    pass


# Methods with these statuses actually need code generated. Members
# have no analogous "status" field in plan.json today -- every member
# is implicitly buildable (its __init__ line starts as a `None` stub
# and needs a real parameter), so there's no member-side filter here.
BUILDABLE_METHOD_STATUSES = ("new", "modified")

# Kept for any external code that still imports the old name.
BUILDABLE_STATUSES = BUILDABLE_METHOD_STATUSES


@dataclass
class Node:
    """Unified node type for both members and methods. `kind`
    distinguishes them; `status` is meaningful for methods
    ("new"/"modified"/"existing_unchanged"/"removed") and is always
    "new" for members, since every member is buildable."""
    id: str
    name: str
    kind: str                  # "member" | "method"
    status: str
    depends_on: list           # raw ids from plan.json, unfiltered
    raw: dict                  # the original member/method dict from plan.json

    # -- Back-compat helpers -------------------------------------------------
    # class_coordinator.py (and anything else written against the old
    # MethodNode shape) used `.depends_on_methods` / `.depends_on_members`.
    # Kept as properties, computed against a DAG-wide id->kind map, so old
    # call sites don't have to change just because members can now be
    # dependency targets too.
    def depends_on_methods(self, kind_of: dict) -> list:
        return [d for d in self.depends_on if kind_of.get(d) == "method"]

    def depends_on_members(self, kind_of: dict) -> list:
        return [d for d in self.depends_on if kind_of.get(d) == "member"]


class ClassDAG:
    def __init__(self, plan: dict):
        self.plan = plan
        member_ids = {m["id"] for m in plan.get("members", [])}
        method_ids = {m["id"] for m in plan.get("methods", [])}
        all_ids = member_ids | method_ids

        self.nodes = {}
        self._kind_of = {}

        # Members first (order of insertion into self.nodes doesn't matter
        # for the topo sort, but it does mean member ids resolve to a kind
        # before any method's depends_on is validated below).
        for mem in plan.get("members", []):
            deps = mem.get("depends_on", [])  # not part of today's schema, but
            # tolerated/validated in case a future planner emits member->member deps
            unknown = [d for d in deps if d not in all_ids]
            if unknown:
                raise UnknownDependencyError(
                    f"Member '{mem['name']}' depends_on unknown id(s): {unknown}"
                )
            self.nodes[mem["id"]] = Node(
                id=mem["id"], name=mem["name"], kind="member",
                status="new", depends_on=deps, raw=mem,
            )
            self._kind_of[mem["id"]] = "member"

        for meth in plan.get("methods", []):
            deps = meth.get("depends_on", [])
            unknown = [d for d in deps if d not in all_ids]
            if unknown:
                raise UnknownDependencyError(
                    f"Method '{meth['name']}' depends_on unknown id(s): {unknown}"
                )
            self.nodes[meth["id"]] = Node(
                id=meth["id"], name=meth["name"], kind="method",
                status=meth.get("status", "new"), depends_on=deps, raw=meth,
            )
            self._kind_of[meth["id"]] = "method"

    def kind_of(self, node_id: str) -> str:
        return self._kind_of.get(node_id)

    def buildable_nodes(self) -> list:
        """Members are always buildable (every one starts as a `None`
        stub in __init__). Methods are buildable only in
        BUILDABLE_METHOD_STATUSES, same rule as before."""
        result = []
        for n in self.nodes.values():
            if n.kind == "member":
                result.append(n)
            elif n.status in BUILDABLE_METHOD_STATUSES:
                result.append(n)
        return result

    def topo_order(self) -> list:
        """Kahn's algorithm over buildable nodes only (members + buildable
        methods), in one unified order. A dependency on a non-buildable
        method (existing_unchanged/removed) doesn't gate anything, same
        as before -- that method's real code (or its absence) is already
        fixed in the skeleton before the loop starts. A dependency on a
        member always gates, since members are always buildable now."""
        buildable_ids = {n.id for n in self.buildable_nodes()}
        indegree = {nid: 0 for nid in buildable_ids}
        dependents = {nid: [] for nid in buildable_ids}

        for nid in buildable_ids:
            for dep in self.nodes[nid].depends_on:
                if dep in buildable_ids:
                    dependents[dep].append(nid)
                    indegree[nid] += 1

        # Tie-break: members before methods when both are otherwise ready,
        # then alphabetical by name -- keeps the "members build first"
        # intent visible in the printed build order even when nothing
        # in depends_on strictly forces it.
        def sort_key(nid):
            return (0 if self.nodes[nid].kind == "member" else 1, self.nodes[nid].name)

        queue = sorted([nid for nid, d in indegree.items() if d == 0], key=sort_key)
        order = []
        while queue:
            nid = queue.pop(0)
            order.append(self.nodes[nid])
            newly_ready = []
            for dep_of in dependents[nid]:
                indegree[dep_of] -= 1
                if indegree[dep_of] == 0:
                    newly_ready.append(dep_of)
            queue.extend(newly_ready)
            queue.sort(key=sort_key)

        if len(order) != len(buildable_ids):
            remaining = buildable_ids - {n.id for n in order}
            names = [self.nodes[nid].name for nid in remaining]
            raise CycleError(f"Cycle detected among member/method dependencies: {names}")

        return order

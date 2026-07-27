"""The unified VectorDBService facade.

The service is the application-facing storage boundary.  A concrete
``ScopedVectorStore`` only knows how to store records and execute vector/payload
queries; this module stores graph nodes and edges as ordinary records and
composes bounded graph traversal from those primitives.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .graph import GraphTableNames, with_graph_tables
from .models import (
    BackendCapabilities,
    BackendRequirements,
    FilterExpression,
    FilterGroup,
    GraphEdge,
    GraphNode,
    GraphNodeRef,
    GraphPath,
    GraphStep,
    GraphTraversalQuery,
    GraphTraversalResult,
    Page,
    Predicate,
    Record,
    RecordQuery,
    TraversedGraphEdge,
    VectorHit,
    VectorQuery,
)
from .registry import TableRegistry
from .scope import DatabaseScope
from .vector_store import ScopedVectorStore

_MISSING = object()


class VectorDBService:
    """Unified vector retrieval and graph-retrieval service.

    Business code depends on this service rather than on a concrete driver or
    on separate record/vector/graph stores.  The backend remains injectable so
    the service can be tested without a database connection.
    """

    def __init__(
        self,
        backend: ScopedVectorStore,
        *,
        graph_enabled: bool = False,
        graph_tables: GraphTableNames | None = None,
        node_tables: Mapping[str, str] | None = None,
    ) -> None:
        self._backend = backend
        self._graph_enabled = graph_enabled
        self._graph_tables = graph_tables or GraphTableNames()
        self._node_tables = dict(node_tables or {})
        if any(not kind or not table for kind, table in self._node_tables.items()):
            raise ValueError("graph node kinds and source table names must not be empty")

    @property
    def name(self) -> str:
        return self._backend.name

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._backend.capabilities

    @property
    def graph_enabled(self) -> bool:
        return self._graph_enabled

    async def ensure_schema(self, tables: TableRegistry) -> None:
        if self._graph_enabled:
            tables = with_graph_tables(tables, table_names=self._graph_tables)
        await self._backend.ensure_schema(tables)

    async def close(self) -> None:
        await self._backend.close()

    async def upsert_records(self, table: str, records: Sequence[Record]) -> None:
        await self._backend.upsert_records(table, records)

    async def get_records(
        self,
        table: str,
        scope: DatabaseScope,
        record_ids: Sequence[str],
        *,
        with_vectors: bool = False,
    ) -> list[Record]:
        return await self._backend.get_records(table, scope, record_ids, with_vectors=with_vectors)

    async def patch_record(
        self,
        table: str,
        scope: DatabaseScope,
        record_id: str,
        changes: Mapping[str, Any],
    ) -> None:
        await self._backend.patch_record(table, scope, record_id, changes)

    async def delete_records(self, table: str, scope: DatabaseScope, record_ids: Sequence[str]) -> None:
        await self._backend.delete_records(table, scope, record_ids)

    async def query_records(self, table: str, query: RecordQuery) -> tuple[list[Record], str | None]:
        return await self._backend.query_records(table, query)

    async def scroll(
        self,
        table: str,
        query: RecordQuery,
        *,
        with_vectors: bool = False,
    ) -> tuple[list[Record], str | None]:
        """Scroll filtered records through the selected backend."""

        return await self._backend.scroll(table, query, with_vectors=with_vectors)

    async def search_vectors(self, query: VectorQuery) -> list[VectorHit]:
        return await self._backend.search_vectors(query)

    async def upsert_nodes(self, nodes: Sequence[GraphNode]) -> None:
        self._require_graph_primitives()
        records = [self._node_to_record(node) for node in nodes]
        if records:
            await self._backend.upsert_records(self._graph_tables.node_table, records)

    async def upsert_edges(self, edges: Sequence[GraphEdge]) -> None:
        if not edges:
            return
        self._require_graph_primitives()
        refs = _unique_refs(ref for edge in edges for ref in (edge.source, edge.target))
        existing = await self._get_nodes(tuple(refs))
        missing = [ref for ref in refs if ref not in existing]
        if missing:
            raise ValueError(f"graph edge endpoints do not exist: {', '.join(_ref_label(ref) for ref in missing)}")
        records = [self._edge_to_record(edge) for edge in edges]
        await self._backend.upsert_records(self._graph_tables.edge_table, records)

    async def delete_edges(self, edges: Sequence[GraphEdge]) -> None:
        """Delete graph edges by their canonical identity, scoped per edge."""

        if not edges:
            return
        self._require_graph_primitives()
        grouped: dict[DatabaseScope, list[str]] = defaultdict(list)
        for edge in edges:
            grouped[edge.scope].append(_edge_record_id(edge))
        for scope, record_ids in grouped.items():
            await self._backend.delete_records(self._graph_tables.edge_table, scope, record_ids)

    async def delete_node(self, ref: GraphNodeRef, *, detach: bool = True) -> None:
        self._require_graph_primitives()
        incident, incident_truncated = await self._query_incident_edges(ref, limit=1 if not detach else 10_000)
        if incident_truncated:
            raise RuntimeError("cannot safely detach graph node because incident edge query was truncated")
        if incident and not detach:
            raise ValueError(f"graph node has {len(incident)} incident edge(s)")
        if incident:
            await self._backend.delete_records(
                self._graph_tables.edge_table,
                ref.scope,
                [_edge_record_id(edge) for edge in incident],
            )
        await self._backend.delete_records(
            self._graph_tables.node_table,
            ref.scope,
            [_node_record_id(ref)],
        )

    async def traverse(self, query: GraphTraversalQuery) -> GraphTraversalResult:
        """Run bounded graph traversal using only record queries and batch reads."""

        self._require_graph_primitives()
        started = time.monotonic()
        seed_nodes = await self._get_nodes(query.seeds)
        paths = [
            GraphPath(seed=seed, nodes=(seed_nodes[seed],), edges=()) for seed in query.seeds if seed in seed_nodes
        ]
        truncated = len(paths) != len(query.seeds)
        expanded_nodes = 0

        for step in query.steps:
            segment_paths: list[GraphPath] = []
            frontier = paths
            for hop in range(1, step.max_hops + 1):
                if not frontier:
                    break
                if _timed_out(started, query.timeout_ms):
                    truncated = True
                    break
                remaining_budget = query.max_expansions - expanded_nodes
                if remaining_budget <= 0:
                    truncated = True
                    break
                if len(frontier) > remaining_budget:
                    frontier = frontier[:remaining_budget]
                    truncated = True
                expanded_nodes += len(frontier)
                frontier, expansion_truncated = await self._expand(
                    frontier,
                    step,
                    query.scope,
                    path_uniqueness=query.path_uniqueness,
                )
                truncated = truncated or expansion_truncated
                if hop >= step.min_hops:
                    segment_paths.extend(frontier)
                if _timed_out(started, query.timeout_ms):
                    truncated = True
                    break
            paths = _dedupe_paths(segment_paths, result_uniqueness="path")
            if not paths:
                break

        if query.result_uniqueness == "end_node":
            paths = _dedupe_paths(paths, result_uniqueness="end_node")

        paths = _sort_paths(paths, query)
        if query.limit_per_seed is not None:
            paths, limited = _limit_per_seed(paths, query.limit_per_seed)
            truncated = truncated or limited
        if len(paths) > query.limit:
            paths = paths[: query.limit]
            truncated = True

        return GraphTraversalResult(paths=tuple(paths), truncated=truncated, expanded_nodes=expanded_nodes)

    async def _expand(
        self,
        paths: Sequence[GraphPath],
        step: GraphStep,
        scope: DatabaseScope,
        *,
        path_uniqueness: str,
    ) -> tuple[list[GraphPath], bool]:
        current_refs = _unique_refs(path.end.ref for path in paths)
        path_by_ref: dict[GraphNodeRef, list[GraphPath]] = defaultdict(list)
        for path in paths:
            path_by_ref[path.end.ref].append(path)

        candidate_edges: list[tuple[GraphEdge, str, GraphNodeRef]] = []
        truncated = False
        for direction in _directions(step.direction):
            field = "source_id" if direction == "out" else "target_id"
            type_field = "source_type" if direction == "out" else "target_type"
            filters: list[FilterExpression] = [
                _membership_predicate(field, [ref.node_id for ref in current_refs]),
                _membership_predicate(type_field, [ref.kind for ref in current_refs]),
            ]
            if step.relations:
                filters.append(_membership_predicate("relation", step.relations))
            records, page_truncated = await self._query_all_records(
                self._graph_tables.edge_table,
                scope,
                _and_filters(filters),
                limit=10_000,
            )
            truncated = truncated or page_truncated
            for record in records:
                edge = _record_to_edge(record)
                if not _filter_matches(step.edge_filters, _edge_values(edge)):
                    continue
                current = edge.source if direction == "out" else edge.target
                target = edge.target if direction == "out" else edge.source
                if current not in path_by_ref:
                    continue
                if step.target_kinds and target.kind not in step.target_kinds:
                    continue
                candidate_edges.append((edge, direction, target))

        target_refs = _unique_refs(target for _, _, target in candidate_edges)
        target_nodes = await self._get_nodes(target_refs)
        matching_target_refs = await self._filter_target_refs(tuple(target_nodes), step.target_filters)
        expanded: list[GraphPath] = []
        for edge, direction, target in candidate_edges:
            if target not in target_nodes:
                continue
            if target not in matching_target_refs:
                continue
            current = edge.source if direction == "out" else edge.target
            for path in path_by_ref[current]:
                if step.direction == "both" and edge.source == edge.target and direction == "in":
                    continue
                if path_uniqueness == "node" and target in _unique_node_keys(path):
                    continue
                if path_uniqueness == "edge" and _path_has_edge(path, edge):
                    continue
                expanded.append(
                    GraphPath(
                        seed=path.seed,
                        nodes=(*path.nodes, target_nodes[target]),
                        edges=(*path.edges, TraversedGraphEdge(edge=edge, direction=direction)),
                    )
                )
        return expanded, truncated

    async def _filter_target_refs(
        self,
        refs: Sequence[GraphNodeRef],
        filters: FilterExpression | None,
    ) -> set[GraphNodeRef]:
        if filters is None:
            return set(refs)

        grouped: dict[tuple[DatabaseScope, str], list[GraphNodeRef]] = defaultdict(list)
        for ref in refs:
            grouped[(ref.scope, ref.kind)].append(ref)

        matching: set[GraphNodeRef] = set()
        for (scope, kind), scoped_refs in grouped.items():
            try:
                table = self._node_tables[kind]
            except KeyError as exc:
                raise RuntimeError(f"no source table is configured for graph node kind {kind!r}") from exc
            records = await self._backend.get_records(table, scope, [ref.node_id for ref in scoped_refs])
            matching_ids = {record.record_id for record in records if _filter_matches(filters, record.payload)}
            matching.update(ref for ref in scoped_refs if ref.node_id in matching_ids)
        return matching

    async def _query_all_records(
        self,
        table: str,
        scope: DatabaseScope,
        filters: FilterExpression | None,
        *,
        limit: int,
    ) -> tuple[list[Record], bool]:
        records: list[Record] = []
        cursor: str | None = None
        truncated = False
        while len(records) < limit:
            page_limit = min(512, limit - len(records))
            page, cursor = await self._backend.query_records(
                table,
                RecordQuery(scope=scope, filters=filters, page=Page(limit=page_limit, cursor=cursor)),
            )
            records.extend(page)
            if cursor is None:
                break
            if not page:
                truncated = True
                break
        if cursor is not None:
            truncated = True
        return records[:limit], truncated

    async def _get_nodes(self, refs: Sequence[GraphNodeRef]) -> dict[GraphNodeRef, GraphNode]:
        if not refs:
            return {}
        by_scope: dict[DatabaseScope, list[GraphNodeRef]] = defaultdict(list)
        for ref in refs:
            by_scope[ref.scope].append(ref)
        result: dict[GraphNodeRef, GraphNode] = {}
        for scope, scoped_refs in by_scope.items():
            records = await self._backend.get_records(
                self._graph_tables.node_table,
                scope,
                [_node_record_id(ref) for ref in scoped_refs],
            )
            for record in records:
                node = _record_to_node(record)
                result[node.ref] = node
        return result

    async def _query_incident_edges(self, ref: GraphNodeRef, *, limit: int) -> tuple[list[GraphEdge], bool]:
        filters = FilterGroup(
            operator="or",
            clauses=(
                FilterGroup(
                    operator="and",
                    clauses=(
                        Predicate(field="source_id", op="eq", value=ref.node_id),
                        Predicate(field="source_type", op="eq", value=ref.kind),
                    ),
                ),
                FilterGroup(
                    operator="and",
                    clauses=(
                        Predicate(field="target_id", op="eq", value=ref.node_id),
                        Predicate(field="target_type", op="eq", value=ref.kind),
                    ),
                ),
            ),
        )
        records, truncated = await self._query_all_records(
            self._graph_tables.edge_table, ref.scope, filters, limit=limit
        )
        return [_record_to_edge(record) for record in records], truncated

    def _require_graph_primitives(self) -> None:
        if not self._graph_enabled:
            raise RuntimeError("VectorDBService graph operations are disabled; set graph_enabled=True")
        required = BackendRequirements(metadata_filtering=True, batch_record_io=True)
        missing = required.missing_from(self.capabilities)
        if missing:
            raise RuntimeError(f"VectorDBService graph operations require backend capabilities: {', '.join(missing)}")

    def _node_to_record(self, node: GraphNode) -> Record:
        return Record(
            table=self._graph_tables.node_table,
            record_id=_node_record_id(node.ref),
            scope=node.ref.scope,
            payload={
                "node_id": node.ref.node_id,
                "node_type": node.ref.kind,
            },
        )

    def _edge_to_record(self, edge: GraphEdge) -> Record:
        return Record(
            table=self._graph_tables.edge_table,
            record_id=_edge_record_id(edge),
            scope=edge.scope,
            payload={
                "source_id": edge.source.node_id,
                "source_type": edge.source.kind,
                "target_id": edge.target.node_id,
                "target_type": edge.target.kind,
                "relation": edge.relation,
                "edge_key": dict(edge.edge_key),
                "properties": dict(edge.properties),
            },
        )


def _node_record_id(ref: GraphNodeRef) -> str:
    return "node:" + _canonical_json((ref.kind, ref.node_id))


def _edge_record_id(edge: GraphEdge) -> str:
    identity = (
        edge.source.kind,
        edge.source.node_id,
        edge.target.kind,
        edge.target.node_id,
        edge.relation,
        edge.edge_key,
    )
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return f"edge:{digest}"


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("graph identity values must be JSON-serializable") from exc


def _ref_label(ref: GraphNodeRef) -> str:
    return f"{ref.kind}:{ref.node_id}"


def _record_to_node(record: Record) -> GraphNode:
    payload = record.payload
    scope = record.scope
    kind = str(payload["node_type"])
    ref = GraphNodeRef(scope=scope, kind=kind, node_id=str(payload["node_id"]))
    return GraphNode(ref=ref)


def _record_to_edge(record: Record) -> GraphEdge:
    payload = record.payload
    source = GraphNodeRef(
        scope=record.scope,
        kind=str(payload["source_type"]),
        node_id=str(payload["source_id"]),
    )
    target = GraphNodeRef(
        scope=record.scope,
        kind=str(payload["target_type"]),
        node_id=str(payload["target_id"]),
    )
    return GraphEdge(
        source=source,
        target=target,
        relation=str(payload["relation"]),
        edge_key=dict(payload.get("edge_key", {})),
        properties=dict(payload.get("properties", {})),
    )


def _node_values(node: GraphNode) -> dict[str, Any]:
    return {"node_id": node.ref.node_id, "node_type": node.ref.kind}


def _edge_values(edge: GraphEdge) -> dict[str, Any]:
    return {
        "source_id": edge.source.node_id,
        "source_type": edge.source.kind,
        "target_id": edge.target.node_id,
        "target_type": edge.target.kind,
        "relation": edge.relation,
        "edge_key": edge.edge_key,
        "properties": edge.properties,
    }


def _lookup(values: Mapping[str, Any], field: str) -> Any:
    if field.startswith("key."):
        field = "edge_key." + field.removeprefix("key.")
    if field in values:
        return values[field]
    parts = field.split(".")
    current: Any = values
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            break
        current = current[part]
    else:
        return current
    if "properties" in values and field not in {"properties", "edge_key"}:
        properties = values["properties"]
        if isinstance(properties, Mapping) and field in properties:
            return properties[field]
    return _MISSING


def _filter_matches(expression: FilterExpression | None, values: Mapping[str, Any]) -> bool:
    if expression is None:
        return True
    if isinstance(expression, Predicate):
        actual = _lookup(values, expression.field)
        expected = expression.value
        if expression.op == "is_null":
            return actual is _MISSING or actual is None
        if expression.op == "is_empty":
            return actual is _MISSING or actual is None or actual == "" or actual == [] or actual == {}
        if actual is _MISSING:
            return expression.op in {"ne", "not_in"}
        if expression.op == "eq":
            return actual == expected
        if expression.op == "ne":
            return actual != expected
        if expression.op in {"gt", "gte", "lt", "lte"}:
            try:
                return {
                    "gt": actual > expected,
                    "gte": actual >= expected,
                    "lt": actual < expected,
                    "lte": actual <= expected,
                }[expression.op]
            except TypeError:
                return False
        if expression.op in {"in", "not_in"}:
            try:
                result = actual in expected
            except TypeError:
                result = False
            return result if expression.op == "in" else not result
        if expression.op == "contains":
            try:
                return expected in actual
            except TypeError:
                return False
        if expression.op == "icontains":
            return isinstance(actual, str) and str(expected).lower() in actual.lower()
        raise ValueError(f"unsupported predicate operator: {expression.op!r}")
    if expression.operator == "and":
        return all(_filter_matches(clause, values) for clause in expression.clauses)
    if expression.operator == "or":
        return any(_filter_matches(clause, values) for clause in expression.clauses)
    if expression.operator == "not":
        return not any(_filter_matches(clause, values) for clause in expression.clauses)
    raise ValueError(f"unsupported filter group operator: {expression.operator!r}")


def _and_filters(filters: Sequence[FilterExpression]) -> FilterExpression | None:
    if not filters:
        return None
    if len(filters) == 1:
        return filters[0]
    return FilterGroup(operator="and", clauses=tuple(filters))


def _membership_predicate(field: str, values: Sequence[Any]) -> Predicate:
    values = tuple(dict.fromkeys(values))
    if len(values) == 1:
        return Predicate(field=field, op="eq", value=values[0])
    return Predicate(field=field, op="in", value=values)


def _directions(direction: str) -> tuple[str, ...]:
    return ("out", "in") if direction == "both" else (direction,)


def _unique_refs(refs: Iterable[GraphNodeRef]) -> tuple[GraphNodeRef, ...]:
    result: list[GraphNodeRef] = []
    seen: set[GraphNodeRef] = set()
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            result.append(ref)
    return tuple(result)


def _unique_node_keys(path: GraphPath) -> set[GraphNodeRef]:
    return {node.ref for node in path.nodes}


def _path_has_edge(path: GraphPath, edge: GraphEdge) -> bool:
    identity = _edge_record_id(edge)
    return any(_edge_record_id(item.edge) == identity for item in path.edges)


def _dedupe_paths(paths: Sequence[GraphPath], *, result_uniqueness: str) -> list[GraphPath]:
    seen: set[Any] = set()
    result: list[GraphPath] = []
    for path in paths:
        if result_uniqueness == "end_node":
            key = (path.seed, path.end.ref)
        else:
            key = (
                path.seed,
                tuple(node.ref for node in path.nodes),
                tuple(_edge_record_id(edge.edge) for edge in path.edges),
            )
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _limit_per_seed(paths: Sequence[GraphPath], limit: int) -> tuple[list[GraphPath], bool]:
    counts: dict[GraphNodeRef, int] = defaultdict(int)
    result: list[GraphPath] = []
    limited = False
    for path in paths:
        if counts[path.seed] >= limit:
            limited = True
            continue
        counts[path.seed] += 1
        result.append(path)
    return result, limited


def _sort_paths(paths: Sequence[GraphPath], query: GraphTraversalQuery) -> list[GraphPath]:
    result = list(paths)
    for sort in reversed(query.order_by):
        result.sort(key=lambda path: _sort_key(path, sort), reverse=sort.direction == "desc")
    return result


def _sort_key(path: GraphPath, sort: Any) -> tuple[bool, Any]:
    if sort.scope == "path":
        value = len(path.edges)
    elif sort.scope == "end_node":
        value = _lookup(_node_values(path.end), sort.field)
    else:
        value = _lookup(_edge_values(path.edges[-1].edge), sort.field) if path.edges else _MISSING
    missing = value is _MISSING or value is None
    if sort.nulls == "first":
        null_rank = 0 if missing else 1
    else:
        null_rank = 1 if missing else 0
    if sort.direction == "desc":
        null_rank = 1 - null_rank
    return (null_rank, None if missing else value)


def _timed_out(started: float, timeout_ms: int | None) -> bool:
    return timeout_ms is not None and (time.monotonic() - started) * 1000 >= timeout_ms


__all__ = ["GraphTableNames", "VectorDBService"]

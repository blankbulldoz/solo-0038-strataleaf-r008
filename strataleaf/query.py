from dataclasses import dataclass
from .codec import digest
from .cursors import decode_cursor, encode_cursor
from .errors import InvalidCursor, InvalidDocument, NotFound
from .indexes import equality_candidates, range_candidates
from .mvcc import checked_revision, record, visible_rows, latest_row
from .predicates import equality_terms, matches, range_terms, validate_predicate


@dataclass(frozen=True)
class Page:
    records: list
    revision: int
    cursor: str | None
    plan: dict


class QueryEngine:
    def __init__(self, store):
        self.store = store

    def select(self, collection, predicate=None, limit=100, cursor=None, revision=None, force_scan=False):
        validate_predicate(predicate)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise InvalidDocument("page size must be between 1 and 1000")
        with self.store.read() as connection:
            if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
                raise NotFound("unknown collection")
            fingerprint = digest({"collection": collection, "predicate": predicate})
            after = None
            if cursor is not None:
                payload = decode_cursor(self.store, cursor)
                if payload.get("query") != fingerprint or (revision is not None and revision != payload.get("revision")):
                    raise InvalidCursor("cursor does not belong to this query/read revision")
                revision, after = payload.get("revision"), payload.get("after")
                if not isinstance(after, str):
                    raise InvalidCursor("cursor has no key boundary")
            revision = checked_revision(self.store, revision)
            head = int(self.store.get("head"))
            plan = {"access": "version_scan", "index": None}
            candidates = None
            if force_scan:
                plan["reason"] = "force_scan requested"
            elif revision != head:
                # Indexes project only the current head; historical reads
                # keep the original fixed-revision version-scan semantics.
                plan["reason"] = "historical revision uses a fixed read view"
            else:
                plan = self._choose_index(connection, collection, predicate)
                candidates = plan.pop("candidates", None)
            if candidates is None:
                rows = visible_rows(connection, collection, revision)
            else:
                rows = [latest_row(connection, collection, key, revision) for key in sorted(candidates)]
            output = []
            examined = 0
            for row in rows:
                if row is None or row["body"] is None or (after is not None and row["key"] <= after):
                    continue
                examined += 1
                item = record(row)
                if matches(item.document, predicate):
                    output.append(item)
                    if len(output) > limit:
                        break
            continuation = None
            if len(output) > limit:
                output = output[:limit]
                continuation = encode_cursor(self.store, {"database": self.store.get("database_id"),
                    "query": fingerprint, "revision": revision, "after": output[-1].key})
            return Page(output, revision, continuation, {**plan, "examined": examined})

    def _choose_index(self, connection, collection, predicate):
        # Equality indexes remain the preferred access path because their
        # tokens identify exact values (and enforce uniqueness).
        for path, value in equality_terms(predicate):
            indexed = equality_candidates(connection, collection, path, value)
            if indexed is not None:
                index_name, keys = indexed
                return {"access": "equality_index", "index": index_name, "candidates": keys}
        groups = range_terms(predicate)
        for path in sorted(groups):
            indexed = range_candidates(connection, collection, groups[path])
            if indexed is None:
                continue
            index_name, keys, detail = indexed
            if detail.get("unindexable"):
                return {"access": "version_scan", "index": None,
                        "reason": "range leaf compares against an unindexable value"}
            return {"access": "range_index", "index": index_name, "bounds": detail["bounds"],
                    "candidates": keys}
        if predicate is None:
            reason = "no predicate"
        elif groups:
            reason = "no matching range index"
        else:
            reason = "predicate has no conjunctive comparison bound"
        return {"access": "version_scan", "index": None, "reason": reason}

from .codec import field, index_token, loads, name, range_token, scalar_token
from .errors import Conflict, InvalidDocument, NotFound
from .mvcc import visible_rows

INDEX_KINDS = ("equality", "range")
RANGE_OPS = ("lt", "le", "gt", "ge")


def refresh_document(connection, collection, key, document):
    connection.execute("DELETE FROM index_values WHERE collection=? AND key=?", (collection, key))
    if document is None:
        return
    for definition in connection.execute("SELECT * FROM index_defs WHERE collection=?", (collection,)).fetchall():
        token = index_token(definition["kind"], field(document, definition["field"]))
        if token is None:
            continue
        if definition["unique_flag"]:
            existing = connection.execute("SELECT key FROM index_values WHERE collection=? AND name=? AND token=? LIMIT 1",
                                          (collection, definition["name"], token)).fetchone()
            if existing:
                raise Conflict("unique index conflict: " + definition["name"])
        connection.execute("INSERT INTO index_values VALUES (?,?,?,?)", (collection, definition["name"], token, key))


class Indexes:
    def __init__(self, store):
        self.store = store

    def create(self, collection, index_name, path, unique=False, kind="equality"):
        name(index_name)
        field({}, path)
        if not isinstance(unique, bool):
            raise InvalidDocument("unique must be boolean")
        if kind not in INDEX_KINDS:
            raise InvalidDocument("index kind must be equality or range")
        if kind == "range" and unique:
            raise InvalidDocument("range indexes do not support the unique constraint")
        with self.store.atomic(writable=True) as connection:
            if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
                raise NotFound("unknown collection")
            if connection.execute("SELECT 1 FROM index_defs WHERE collection=? AND name=?", (collection, index_name)).fetchone():
                raise Conflict("index already exists")
            connection.execute("INSERT INTO index_defs(collection,name,field,kind,unique_flag) VALUES (?,?,?,?,?)",
                               (collection, index_name, path, kind, int(unique)))
            seen = set()
            for row in visible_rows(connection, collection, int(self.store.get("head"))):
                token = index_token(kind, field(loads(row["body"]), path))
                if token is None:
                    continue
                if unique and token in seen:
                    raise Conflict("existing documents violate unique index")
                seen.add(token)
                connection.execute("INSERT INTO index_values VALUES (?,?,?,?)", (collection, index_name, token, row["key"]))
            self.store.set("catalog_epoch", int(self.store.get("catalog_epoch")) + 1)
        return {"name": index_name, "field": path, "kind": kind, "unique": unique}

    def drop(self, collection, index_name):
        with self.store.atomic(writable=True) as connection:
            changed = connection.execute("DELETE FROM index_defs WHERE collection=? AND name=?", (collection, index_name)).rowcount
            if not changed:
                raise NotFound("unknown index")
            self.store.set("catalog_epoch", int(self.store.get("catalog_epoch")) + 1)

    def list(self, collection):
        with self.store.read() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM index_defs WHERE collection=? ORDER BY name", (collection,))]


def equality_candidates(connection, collection, path, value):
    token = scalar_token(value)
    if token is None:
        return None
    definition = connection.execute(
        "SELECT name FROM index_defs WHERE collection=? AND field=? AND kind='equality' ORDER BY name LIMIT 1",
        (collection, path)).fetchone()
    if definition is None:
        return None
    keys = {row[0] for row in connection.execute("SELECT key FROM index_values WHERE collection=? AND name=? AND token=?",
                                               (collection, definition[0], token))}
    return definition[0], keys


def range_candidates(connection, collection, terms):
    """Resolve indexable leaf terms on a single range-indexed field.

    terms is a list of (path, op, value) leaves. Returns
    (index_name, keys, detail) or None when no range index serves the field.
    A non-indexable leaf value makes the predicate unsatisfiable through this
    index and yields an empty candidate set.
    """
    path = terms[0][0]
    definition = connection.execute(
        "SELECT name FROM index_defs WHERE collection=? AND field=? AND kind='range' ORDER BY name LIMIT 1",
        (collection, path)).fetchone()
    if definition is None:
        return None
    clauses, parameters = [], []
    for _field_path, op, value in terms:
        if op == "eq":
            token = range_token(value)
            if token is None:
                return definition[0], set(), {"bounds": [], "unindexable": True}
            clauses.append("token=?")
            parameters.append(token)
        elif op in RANGE_OPS:
            token = range_token(value)
            if token is None:
                return definition[0], set(), {"bounds": [], "unindexable": True}
            # Bounds are inclusive so that values sharing the boundary token
            # (e.g. 1 and 1.0) are never skipped; the predicate re-checks
            # strictness against the original document.
            clauses.append("token<=?" if op in ("lt", "le") else "token>=?")
            parameters.append(token)
        else:
            return None
    if not clauses:
        return None
    sql = "SELECT key FROM index_values WHERE collection=? AND name=? AND " + " AND ".join(clauses)
    keys = {row[0] for row in connection.execute(sql, (collection, definition[0], *parameters))}
    bounds = [{"op": op, "inclusive": op in ("le", "ge", "eq"), "value": value} for _, op, value in terms]
    return definition[0], keys, {"bounds": bounds}

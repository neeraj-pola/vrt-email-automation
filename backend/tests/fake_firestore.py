"""
A small in-memory stand-in for the Firestore client -- only the calls
app/services/firestore_stores.py makes: documents, set/update/get, where()
with ==, in, <= and >=, select(), limit(), stream(), count(), batches and
transactions. Lets the Firestore stores run their real code in tests with no
network and no emulator, and be checked against the in-memory stores.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace


class FakeSnapshot:
    def __init__(self, ref, data):
        self.reference = ref
        self.id = ref.id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None

    def get(self, field):
        if self._data is None or field not in self._data:
            raise KeyError(field)
        return copy.deepcopy(self._data[field])


class FakeDocRef:
    def __init__(self, col, doc_id):
        self._col, self.id = col, doc_id

    def get(self, transaction=None):
        return FakeSnapshot(self, self._col.docs.get(self.id))

    def set(self, data, merge=False):
        if merge and self.id in self._col.docs:
            self._col.docs[self.id] = {**self._col.docs[self.id], **copy.deepcopy(data)}
        else:
            self._col.docs[self.id] = copy.deepcopy(data)

    def update(self, data):
        if self.id not in self._col.docs:
            raise KeyError(self.id)
        self._col.docs[self.id].update(copy.deepcopy(data))


def _matches(value, op, target) -> bool:
    if value is None:
        return False  # null never satisfies an equality-to-value, range or `in` filter on a non-null target
    if op == "==":
        return value == target
    if op == "in":
        return value in target
    if op == "<=":
        return value <= target
    if op == ">=":
        return value >= target
    raise NotImplementedError(op)


class FakeQuery:
    def __init__(self, col, filters=(), fields=None, limit=None):
        self._col, self._filters, self._fields, self._limit = col, list(filters), fields, limit
        self.reads = 0

    def where(self, *, filter):
        return FakeQuery(self._col, [*self._filters, filter], self._fields, self._limit)

    def select(self, fields):
        return FakeQuery(self._col, self._filters, list(fields), self._limit)

    def limit(self, n):
        return FakeQuery(self._col, self._filters, self._fields, n)

    def _rows(self):
        out = []
        for doc_id, data in self._col.docs.items():
            if all(f.field_path in data and _matches(data[f.field_path], f.op_string, f.value) for f in self._filters):
                out.append((doc_id, data))
        return out[: self._limit] if self._limit is not None else out

    def stream(self):
        rows = self._rows()
        self._col.client.reads += len(rows)  # Firestore bills per document returned
        for doc_id, data in rows:
            shown = data if self._fields is None else {k: v for k, v in data.items() if k in self._fields}
            yield FakeSnapshot(FakeDocRef(self._col, doc_id), copy.deepcopy(shown))

    def count(self):
        n = len(self._rows())
        return SimpleNamespace(get=lambda: [[SimpleNamespace(value=n)]])


class FakeCollection(FakeQuery):
    def __init__(self, client, name):
        self.client, self.name, self.docs = client, name, {}
        super().__init__(self, (), None, None)

    def document(self, doc_id):
        return FakeDocRef(self, doc_id)


class FakeBatch:
    def __init__(self):
        self._ops = []

    def set(self, ref, data):
        self._ops.append((ref, data))

    def commit(self):
        assert len(self._ops) <= 500, "Firestore allows at most 500 writes per batch"
        for ref, data in self._ops:
            ref.set(data)


class FakeTransaction:
    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)


class FakeFirestore:
    def __init__(self):
        self._cols = {}
        self.reads = 0  # documents read through stream(), to assert on read budgets

    def collection(self, name):
        return self._cols.setdefault(name, FakeCollection(self, name))

    def batch(self):
        return FakeBatch()

    def transaction(self):
        return FakeTransaction()

"""In-memory DynamoDB fake with the low-level client interface the platform uses.

Why a fake when moto exists: the repository tests also run against moto
(``tests/unit/test_repository.py`` parametrizes both), but the fake adds what moto cannot:

* deterministic **fault injection** (``fail_next``: throttle, crash before/after a
  transaction, ``TransactionConflict``),
* **hooks** that run inside a transaction's critical section, to force interleavings
  for concurrency tests (two writers that both read revision 4),
* call recording (``calls``) so tests assert that nothing was written.

Supported operations: ``create_table``, ``describe_table``, ``put_item``, ``get_item``,
``update_item``, ``delete_item``, ``query`` (table or GSI; ``KeyConditionExpression``,
``FilterExpression``, ``ScanIndexForward``, ``Limit``, ``ExclusiveStartKey``), ``scan``,
``transact_write_items`` (``Put``/``Update``/``Delete``/``ConditionCheck``) and
``transact_get_items``.

Expressions: ``attribute_exists``, ``attribute_not_exists``, ``begins_with``,
``contains``, comparisons ``= <> < <= > >=``, ``BETWEEN``, ``IN``, ``AND``/``OR``/``NOT``
and parentheses; updates ``SET a = v``, ``SET a = b + v``/``- v``,
``SET a = if_not_exists(a, v)``, ``REMOVE``, ``ADD``. Attribute paths are top-level names
(``#name`` placeholders or plain identifiers).

Errors are ``botocore.exceptions.ClientError`` with the real error codes
(``ConditionalCheckFailedException``, ``TransactionCanceledException`` with
``CancellationReasons`` aligned to the request items, ``ResourceNotFoundException``,
``ValidationException``), so repository code behaves identically against the fake,
moto and DynamoDB.
"""

from __future__ import annotations

import copy
import re
import threading
from decimal import Decimal
from typing import Any, Callable, Iterable

from botocore.exceptions import ClientError

__all__ = ["FakeDynamoDB", "ExpressionError"]


class ExpressionError(ValueError):
    pass


def _client_error(code: str, message: str, op: str, **extra: Any) -> ClientError:
    resp: dict[str, Any] = {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": 400}}
    resp.update(extra)
    return ClientError(resp, op)


# ===================================================================== values
def _cmp_key(av: dict[str, Any]) -> tuple[str, Any]:
    ((t, v),) = av.items()
    if t == "N":
        return ("N", Decimal(v))
    if t in ("S", "B"):
        return (t, v)
    return (t, repr(v))


def _eq(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if a is None or b is None:
        return False
    ((ta, va),) = a.items()
    ((tb, vb),) = b.items()
    if ta != tb:
        return False
    if ta == "N":
        return Decimal(va) == Decimal(vb)
    if ta in ("SS", "NS"):
        return set(va) == set(vb)
    return va == vb


# ===================================================================== tokenizer
_TOKEN = re.compile(
    r"\s*(?:(?P<op><>|<=|>=|=|<|>|\(|\)|,|\+|-)|(?P<name>#[A-Za-z0-9_]+)|(?P<value>:[A-Za-z0-9_]+)|(?P<ident>[A-Za-z_][A-Za-z0-9_]*))"
)
_KEYWORDS = {"AND", "OR", "NOT", "IN", "BETWEEN", "SET", "REMOVE", "ADD", "DELETE"}


def _tokenize(expr: str) -> list[tuple[str, str]]:
    pos, out = 0, []
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise ExpressionError(f"cannot parse expression near {expr[pos:pos + 20]!r}")
        pos = m.end()
        if m.group("op"):
            out.append(("op", m.group("op")))
        elif m.group("name"):
            out.append(("name", m.group("name")))
        elif m.group("value"):
            out.append(("value", m.group("value")))
        else:
            ident = m.group("ident")
            out.append(("kw", ident.upper()) if ident.upper() in _KEYWORDS else ("ident", ident))
    return out


class _Parser:
    def __init__(self, expr: str, names: dict[str, str] | None, values: dict[str, Any] | None) -> None:
        self.toks = _tokenize(expr)
        self.i = 0
        self.names = names or {}
        self.values = values or {}
        self.used_names: set[str] = set()
        self.used_values: set[str] = set()

    def peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, kind: str | None = None, value: str | None = None) -> tuple[str, str]:
        t = self.peek()
        if t is None or (kind and t[0] != kind) or (value and t[1] != value):
            raise ExpressionError(f"expected {value or kind}, got {t}")
        self.i += 1
        return t

    def accept(self, kind: str, value: str | None = None) -> bool:
        t = self.peek()
        if t is not None and t[0] == kind and (value is None or t[1] == value):
            self.i += 1
            return True
        return False

    def done(self) -> None:
        if self.peek() is not None:
            raise ExpressionError(f"unexpected token {self.peek()}")

    # ---------------------------------------------------------- operands
    def path(self) -> str:
        t = self.take()
        if t[0] == "name":
            if t[1] not in self.names:
                raise _client_error("ValidationException", f"An expression attribute name used in the document path is not defined; attribute name: {t[1]}", "Expression")
            self.used_names.add(t[1])
            return self.names[t[1]]
        if t[0] == "ident":
            return t[1]
        raise ExpressionError(f"expected an attribute path, got {t}")

    def operand(self) -> tuple[str, Any]:
        t = self.peek()
        if t is None:
            raise ExpressionError("missing operand")
        if t[0] == "value":
            self.i += 1
            if t[1] not in self.values:
                raise _client_error("ValidationException", f"An expression attribute value used in expression is not defined; attribute value: {t[1]}", "Expression")
            self.used_values.add(t[1])
            return ("val", self.values[t[1]])
        return ("path", self.path())

    # ---------------------------------------------------------- conditions
    def condition(self) -> Callable[[dict[str, Any]], bool]:
        return self._or()

    def _or(self) -> Callable[[dict[str, Any]], bool]:
        left = self._and()
        while self.accept("kw", "OR"):
            right = self._and()
            left = (lambda a, b: lambda it: a(it) or b(it))(left, right)
        return left

    def _and(self) -> Callable[[dict[str, Any]], bool]:
        left = self._not()
        while self.accept("kw", "AND"):
            right = self._not()
            left = (lambda a, b: lambda it: a(it) and b(it))(left, right)
        return left

    def _not(self) -> Callable[[dict[str, Any]], bool]:
        if self.accept("kw", "NOT"):
            inner = self._not()
            return lambda it: not inner(it)
        return self._primary()

    def _primary(self) -> Callable[[dict[str, Any]], bool]:
        if self.accept("op", "("):
            inner = self.condition()
            self.take("op", ")")
            return inner
        t = self.peek()
        if t and t[0] == "ident" and t[1] in ("attribute_exists", "attribute_not_exists", "begins_with", "contains", "attribute_type"):
            fn = t[1]
            self.i += 1
            self.take("op", "(")
            p = self.path()
            arg = None
            if fn in ("begins_with", "contains", "attribute_type"):
                self.take("op", ",")
                arg = self.operand()
            self.take("op", ")")
            if fn == "attribute_exists":
                return lambda it: p in it
            if fn == "attribute_not_exists":
                return lambda it: p not in it
            if fn == "begins_with":
                return lambda it: (p in it and "S" in it[p] and _resolve(arg, it) is not None and it[p]["S"].startswith(_resolve(arg, it)["S"]))  # type: ignore[index]
            if fn == "contains":
                return lambda it: p in it and _contains(it[p], _resolve(arg, it))
            return lambda it: p in it and next(iter(it[p])) == next(iter(_resolve(arg, it).values()))  # type: ignore[union-attr]
        left = self.operand()
        if self.accept("kw", "BETWEEN"):
            lo = self.operand()
            self.take("kw", "AND")
            hi = self.operand()
            return lambda it: _between(_resolve(left, it), _resolve(lo, it), _resolve(hi, it))
        if self.accept("kw", "IN"):
            self.take("op", "(")
            opts = [self.operand()]
            while self.accept("op", ","):
                opts.append(self.operand())
            self.take("op", ")")
            return lambda it: any(_eq(_resolve(left, it), _resolve(o, it)) for o in opts)
        op = self.take("op")[1]
        right = self.operand()
        return lambda it: _compare(op, _resolve(left, it), _resolve(right, it))

    # ---------------------------------------------------------- updates
    def update(self) -> list[tuple[str, str, Any]]:
        actions: list[tuple[str, str, Any]] = []
        while self.peek() is not None:
            kw = self.take("kw")[1]
            while True:
                if kw == "SET":
                    p = self.path()
                    self.take("op", "=")
                    actions.append(("SET", p, self._set_value()))
                elif kw == "REMOVE":
                    actions.append(("REMOVE", self.path(), None))
                elif kw in ("ADD", "DELETE"):
                    p = self.path()
                    actions.append((kw, p, self.operand()))
                else:
                    raise ExpressionError(f"unsupported update clause {kw}")
                if not self.accept("op", ","):
                    break
        return actions

    def _set_value(self) -> Any:
        t = self.peek()
        if t and t[0] == "ident" and t[1] in ("if_not_exists", "list_append"):
            fn = t[1]
            self.i += 1
            self.take("op", "(")
            a = self.operand()
            self.take("op", ",")
            b = self.operand()
            self.take("op", ")")
            base: Any = ("fn", fn, a, b)
        else:
            base = self.operand()
        if self.accept("op", "+"):
            return ("arith", "+", base, self.operand())
        if self.accept("op", "-"):
            return ("arith", "-", base, self.operand())
        return base


def _resolve(operand: Any, item: dict[str, Any]) -> dict[str, Any] | None:
    if operand is None:
        return None
    kind = operand[0]
    if kind == "val":
        return operand[1]
    if kind == "path":
        return item.get(operand[1])
    if kind == "fn":
        _, fn, a, b = operand
        if fn == "if_not_exists":
            assert a[0] == "path"
            return item[a[1]] if a[1] in item else _resolve(b, item)
        la, lb = _resolve(a, item), _resolve(b, item)
        return {"L": list((la or {"L": []})["L"]) + list((lb or {"L": []})["L"])}
    if kind == "arith":
        _, op, a, b = operand
        va, vb = _resolve(a, item), _resolve(b, item)
        if va is None or vb is None or "N" not in va or "N" not in vb:
            raise _client_error("ValidationException", "An operand in the update expression has an incorrect data type", "UpdateItem")
        x, y = Decimal(va["N"]), Decimal(vb["N"])
        return {"N": str(x + y if op == "+" else x - y)}
    raise ExpressionError(f"bad operand {operand}")


def _compare(op: str, a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if op == "=":
        return _eq(a, b)
    if op == "<>":
        return not _eq(a, b)
    if a is None or b is None:
        return False
    ka, kb = _cmp_key(a), _cmp_key(b)
    if ka[0] != kb[0]:
        return False
    return {"<": ka[1] < kb[1], "<=": ka[1] <= kb[1], ">": ka[1] > kb[1], ">=": ka[1] >= kb[1]}[op]


def _between(v: dict[str, Any] | None, lo: dict[str, Any] | None, hi: dict[str, Any] | None) -> bool:
    return _compare(">=", v, lo) and _compare("<=", v, hi)


def _contains(container: dict[str, Any], needle: dict[str, Any] | None) -> bool:
    if needle is None:
        return False
    ((t, v),) = container.items()
    if t == "S" and "S" in needle:
        return needle["S"] in v
    if t in ("SS", "NS"):
        return next(iter(needle.values())) in v
    if t == "L":
        return any(_eq(x, needle) for x in v)
    return False


# ===================================================================== tables
class _Table:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.name = spec["TableName"]
        self.spec = copy.deepcopy(spec)
        ks = {k["KeyType"]: k["AttributeName"] for k in spec["KeySchema"]}
        self.hash_key = ks["HASH"]
        self.range_key = ks.get("RANGE")
        self.indexes: dict[str, tuple[str, str | None]] = {}
        for gsi in spec.get("GlobalSecondaryIndexes", []) or []:
            gks = {k["KeyType"]: k["AttributeName"] for k in gsi["KeySchema"]}
            self.indexes[gsi["IndexName"]] = (gks["HASH"], gks.get("RANGE"))
        self.items: dict[tuple[str, str | None], dict[str, Any]] = {}

    def key_of(self, item_or_key: dict[str, Any]) -> tuple[str, str | None]:
        try:
            h = next(iter(item_or_key[self.hash_key].values()))
            r = next(iter(item_or_key[self.range_key].values())) if self.range_key else None
        except KeyError:
            raise _client_error("ValidationException", "The provided key element does not match the schema", "Key") from None
        return (str(h), None if r is None else str(r))


class FakeDynamoDB:
    """See module docstring."""

    def __init__(self) -> None:
        self._tables: dict[str, _Table] = {}
        self._lock = threading.RLock()
        self._faults: list[tuple[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: callables run inside transact_write_items after conditions pass, before applying writes
        self.before_apply_hooks: list[Callable[[list[dict[str, Any]]], None]] = []
        #: callables run inside transact_write_items before conditions are evaluated (outside the lock)
        self.before_transact_hooks: list[Callable[[list[dict[str, Any]]], None]] = []

    # ---------------------------------------------------------- fault injection
    def fail_next(self, operation: str, error: str | BaseException = "ThrottlingException", *, after: bool = False) -> None:
        """Make the next ``operation`` call fail. ``after=True`` applies the writes first and then fails
        (a crash after commit: the caller sees an error although the write landed)."""
        self._faults.append((operation, (error, after)))

    def _maybe_fail(self, operation: str, phase: str) -> None:
        for i, (op, (err, after)) in enumerate(self._faults):
            if op == operation and (after == (phase == "after")):
                del self._faults[i]
                if isinstance(err, BaseException):
                    raise err
                raise _client_error(err, f"injected {err}", operation)

    # ---------------------------------------------------------- tables
    def create_table(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            name = kwargs["TableName"]
            if name in self._tables:
                raise _client_error("ResourceInUseException", f"Table already exists: {name}", "CreateTable")
            self._tables[name] = _Table(kwargs)
        return {"TableDescription": {"TableName": name, "TableStatus": "ACTIVE"}}

    def describe_table(self, TableName: str) -> dict[str, Any]:
        t = self._table(TableName, "DescribeTable")
        return {"Table": {"TableName": t.name, "TableStatus": "ACTIVE", "KeySchema": t.spec["KeySchema"], "ItemCount": len(t.items)}}

    def _table(self, name: str, op: str) -> _Table:
        t = self._tables.get(name)
        if t is None:
            raise _client_error("ResourceNotFoundException", "Requested resource not found", op)
        return t

    def table_items(self, name: str) -> list[dict[str, Any]]:
        """Test helper: deep copies of every item of a table."""
        with self._lock:
            return [copy.deepcopy(v) for v in self._tables[name].items.values()]

    # ---------------------------------------------------------- single-item ops
    def _check(self, item: dict[str, Any] | None, expr: str | None, names: Any, values: Any) -> bool:
        if not expr:
            return True
        p = _Parser(expr, names, values)
        fn = p.condition()
        p.done()
        return bool(fn(item or {}))

    def put_item(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("PutItem", kw))
        self._maybe_fail("PutItem", "before")
        with self._lock:
            t = self._table(kw["TableName"], "PutItem")
            key = t.key_of(kw["Item"])
            old = t.items.get(key)
            if not self._check(old, kw.get("ConditionExpression"), kw.get("ExpressionAttributeNames"), kw.get("ExpressionAttributeValues")):
                extra = {"Item": copy.deepcopy(old)} if old and kw.get("ReturnValuesOnConditionCheckFailure") == "ALL_OLD" else {}
                raise _client_error("ConditionalCheckFailedException", "The conditional request failed", "PutItem", **extra)
            t.items[key] = copy.deepcopy(kw["Item"])
        self._maybe_fail("PutItem", "after")
        return {}

    def get_item(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("GetItem", kw))
        self._maybe_fail("GetItem", "before")
        with self._lock:
            t = self._table(kw["TableName"], "GetItem")
            it = t.items.get(t.key_of(kw["Key"]))
            return {"Item": copy.deepcopy(it)} if it is not None else {}

    def delete_item(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("DeleteItem", kw))
        self._maybe_fail("DeleteItem", "before")
        with self._lock:
            t = self._table(kw["TableName"], "DeleteItem")
            key = t.key_of(kw["Key"])
            old = t.items.get(key)
            if not self._check(old, kw.get("ConditionExpression"), kw.get("ExpressionAttributeNames"), kw.get("ExpressionAttributeValues")):
                raise _client_error("ConditionalCheckFailedException", "The conditional request failed", "DeleteItem")
            t.items.pop(key, None)
        return {}

    def _apply_update(self, t: _Table, key_av: dict[str, Any], expr: str, names: Any, values: Any) -> dict[str, Any]:
        key = t.key_of(key_av)
        item = copy.deepcopy(t.items.get(key)) or copy.deepcopy(key_av)
        p = _Parser(expr, names, values)
        actions = p.update()
        for action, path, operand in actions:
            if path in (t.hash_key, t.range_key):
                raise _client_error("ValidationException", "Cannot update attribute; this attribute is part of the key", "UpdateItem")
            if action == "SET":
                item[path] = _resolve(operand, t.items.get(key) or {})
            elif action == "REMOVE":
                item.pop(path, None)
            elif action == "ADD":
                inc = _resolve(operand, {})
                cur = item.get(path)
                if inc is not None and "N" in inc:
                    item[path] = {"N": str(Decimal(cur["N"] if cur else "0") + Decimal(inc["N"]))}
                elif inc is not None:
                    ((st, sv),) = inc.items()
                    item[path] = {st: sorted(set((cur or {st: []})[st]) | set(sv))}
            elif action == "DELETE":
                inc = _resolve(operand, {})
                cur = item.get(path)
                if cur and inc:
                    ((st, sv),) = inc.items()
                    rest = sorted(set(cur[st]) - set(sv))
                    if rest:
                        item[path] = {st: rest}
                    else:
                        item.pop(path)
        return item

    def update_item(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("UpdateItem", kw))
        self._maybe_fail("UpdateItem", "before")
        with self._lock:
            t = self._table(kw["TableName"], "UpdateItem")
            key = t.key_of(kw["Key"])
            old = t.items.get(key)
            if not self._check(old, kw.get("ConditionExpression"), kw.get("ExpressionAttributeNames"), kw.get("ExpressionAttributeValues")):
                extra = {"Item": copy.deepcopy(old)} if old and kw.get("ReturnValuesOnConditionCheckFailure") == "ALL_OLD" else {}
                raise _client_error("ConditionalCheckFailedException", "The conditional request failed", "UpdateItem", **extra)
            new = self._apply_update(t, kw["Key"], kw["UpdateExpression"], kw.get("ExpressionAttributeNames"), kw.get("ExpressionAttributeValues"))
            t.items[key] = new
            rv = kw.get("ReturnValues", "NONE")
        self._maybe_fail("UpdateItem", "after")
        return {"Attributes": copy.deepcopy(new)} if rv == "ALL_NEW" else {}

    # ---------------------------------------------------------- query / scan
    def query(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("Query", kw))
        self._maybe_fail("Query", "before")
        with self._lock:
            t = self._table(kw["TableName"], "Query")
            index = kw.get("IndexName")
            if index:
                if index not in t.indexes:
                    raise _client_error("ValidationException", f"The table does not have the specified index: {index}", "Query")
                hk, rk = t.indexes[index]
            else:
                hk, rk = t.hash_key, t.range_key
            names, values = kw.get("ExpressionAttributeNames"), kw.get("ExpressionAttributeValues")
            kp = _Parser(kw["KeyConditionExpression"], names, values)
            key_fn = kp.condition()
            kp.done()
            candidates = [it for it in t.items.values() if hk in it and (rk is None or rk in it) and key_fn(it)]
            return self._page(t, candidates, rk, kw, names, values, index_keys=(hk, rk) if index else None)

    def scan(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("Scan", kw))
        self._maybe_fail("Scan", "before")
        with self._lock:
            t = self._table(kw["TableName"], "Scan")
            kw = {**kw, "ScanIndexForward": True}
            return self._page(t, list(t.items.values()), None, kw, kw.get("ExpressionAttributeNames"), kw.get("ExpressionAttributeValues"), index_keys=None)

    def _page(self, t: _Table, candidates: list[dict[str, Any]], rk: str | None, kw: dict[str, Any], names: Any, values: Any, index_keys: tuple[str, str | None] | None) -> dict[str, Any]:
        def sort_key(it: dict[str, Any]) -> tuple[Any, ...]:
            primary = t.key_of(it)
            rkv = _cmp_key(it[rk]) if rk and rk in it else ("", "")
            return (rkv, primary)

        ordered = sorted(candidates, key=sort_key, reverse=not kw.get("ScanIndexForward", True))
        start = kw.get("ExclusiveStartKey")
        if start:
            skey = t.key_of(start)
            idx = next((i for i, it in enumerate(ordered) if t.key_of(it) == skey), None)
            ordered = ordered[idx + 1 :] if idx is not None else []
        limit = kw.get("Limit")
        page = ordered[:limit] if limit else ordered
        more = bool(limit) and len(ordered) > len(page)
        filt = kw.get("FilterExpression")
        if filt:
            fp = _Parser(filt, names, values)
            ffn = fp.condition()
            fp.done()
            items = [it for it in page if ffn(it)]
        else:
            items = page
        out: dict[str, Any] = {"Items": [copy.deepcopy(i) for i in items], "Count": len(items), "ScannedCount": len(page)}
        if more and page:
            last = page[-1]
            lek = {t.hash_key: last[t.hash_key]}
            if t.range_key:
                lek[t.range_key] = last[t.range_key]
            if index_keys:
                for k in index_keys:
                    if k and k in last:
                        lek[k] = last[k]
            out["LastEvaluatedKey"] = copy.deepcopy(lek)
        return out

    # ---------------------------------------------------------- transactions
    def transact_get_items(self, TransactItems: list[dict[str, Any]], **_: Any) -> dict[str, Any]:
        with self._lock:
            out = []
            for ti in TransactItems:
                g = ti["Get"]
                t = self._table(g["TableName"], "TransactGetItems")
                it = t.items.get(t.key_of(g["Key"]))
                out.append({"Item": copy.deepcopy(it)} if it is not None else {})
            return {"Responses": out}

    def transact_write_items(self, TransactItems: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
        self.calls.append(("TransactWriteItems", {"TransactItems": TransactItems, **kw}))
        for hook in list(self.before_transact_hooks):
            hook(TransactItems)
        self._maybe_fail("TransactWriteItems", "before")
        if len(TransactItems) > 100:
            raise _client_error("ValidationException", "Member must have length less than or equal to 100", "TransactWriteItems")
        with self._lock:
            seen: set[tuple[str, tuple[str, str | None]]] = set()
            reasons: list[dict[str, Any]] = []
            failed = False
            plan: list[tuple[str, _Table, dict[str, Any]]] = []
            for ti in TransactItems:
                ((kind, body),) = ti.items()
                t = self._table(body["TableName"], "TransactWriteItems")
                key_src = body.get("Item") if kind == "Put" else body["Key"]
                key = t.key_of(key_src)
                if (t.name, key) in seen:
                    raise _client_error("ValidationException", "Transaction request cannot include multiple operations on one item", "TransactWriteItems")
                seen.add((t.name, key))
                old = t.items.get(key)
                ok = self._check(old, body.get("ConditionExpression"), body.get("ExpressionAttributeNames"), body.get("ExpressionAttributeValues"))
                if ok:
                    reasons.append({"Code": "None"})
                else:
                    failed = True
                    r: dict[str, Any] = {"Code": "ConditionalCheckFailed", "Message": "The conditional request failed"}
                    if old is not None and body.get("ReturnValuesOnConditionCheckFailure") == "ALL_OLD":
                        r["Item"] = copy.deepcopy(old)
                    reasons.append(r)
                plan.append((kind, t, body))
            if failed:
                codes = ", ".join(r["Code"] for r in reasons)
                raise _client_error(
                    "TransactionCanceledException",
                    f"Transaction cancelled, please refer cancellation reasons for specific reasons [{codes}]",
                    "TransactWriteItems",
                    CancellationReasons=reasons,
                )
            for hook in list(self.before_apply_hooks):
                hook(TransactItems)
            # compute every new item first, then apply all (atomic)
            staged: list[tuple[_Table, tuple[str, str | None], dict[str, Any] | None]] = []
            for kind, t, body in plan:
                if kind == "Put":
                    staged.append((t, t.key_of(body["Item"]), copy.deepcopy(body["Item"])))
                elif kind == "Update":
                    staged.append((t, t.key_of(body["Key"]), self._apply_update(t, body["Key"], body["UpdateExpression"], body.get("ExpressionAttributeNames"), body.get("ExpressionAttributeValues"))))
                elif kind == "Delete":
                    staged.append((t, t.key_of(body["Key"]), None))
            for t, key, new in staged:
                if new is None:
                    t.items.pop(key, None)
                else:
                    t.items[key] = new
        self._maybe_fail("TransactWriteItems", "after")
        return {}

    # ---------------------------------------------------------- misc
    def tables(self) -> Iterable[str]:
        return list(self._tables)

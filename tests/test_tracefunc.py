# tests/test_tracefunc.py
import sys
import pytest

# Adjust this import to match where you placed `tracefunc`.
# e.g. from mypkg.trace import tracefunc
from tracefunc import tracefunc


pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="tracefunc requires Python 3.11+ (PEP 657 fine-grained positions).",
)


GLOB = 10


# ----------------------------
# Sample functions to trace
# ----------------------------

def _basic():
    x = 1
    y = x + 2
    return y


def _semicolons():
    x = 1; y = 2; z = x + y; return z


def _for_one_liner():
    out = []
    for i in range(3): out.append(i); out.append(i + 1)
    return out


def _comprehension(n):
    xs = [i * i for i in range(n)]
    return xs


def _comp_expr(n):
    [i for i in range(n)]
    return n


def _nested_not_called():
    def inner(a):
        b = a + 1
        return b
    return 0


def _nested_called():
    def inner(a):
        b = a + 1
        return b
    x = inner(3)
    return x


def _builtin_global(seq):
    g = GLOB
    n = len(seq)
    return g + n


def _del_var():
    x = 1
    del x
    return 0


def _dupes():
    x = 1
    x = 1
    return x


def _many_hits():
    x = 0
    for i in range(20):
        x += i
    return x


def _class_and_method():
    class C:
        y = 7

        def inc(self, z):
            t = z + self.y
            return t

    c = C()
    r = c.inc(5)
    return r


def _raises():
    x = 1
    raise ValueError("boom")


# ----------------------------
# Helpers
# ----------------------------

def _match_keys(res: dict, snippet: str) -> list[str]:
    """Return all keys whose stripped text equals `snippet`."""
    keys = [k for k in res.keys() if k.strip() == snippet]
    assert keys, f"Did not find a key matching snippet: {snippet!r}. Keys were: {sorted(res)!r}"
    return keys


def _get_entry(res: dict, snippet: str, *, nth: int = 0):
    """Return (key, (count, vars_map)) for the nth matching key."""
    keys = _match_keys(res, snippet)
    assert nth < len(keys), f"Only {len(keys)} matches for {snippet!r}, requested nth={nth}."
    k = keys[nth]
    return k, res[k]


def _assert_shape(res: dict):
    assert isinstance(res, dict)
    for k, v in res.items():
        assert isinstance(k, str)
        assert isinstance(v, tuple) and len(v) == 2
        count, vars_map = v
        assert isinstance(count, int) and count >= 0
        assert isinstance(vars_map, dict)
        for name, samples in vars_map.items():
            assert isinstance(name, str)
            assert isinstance(samples, list)
            for tup in samples:
                assert isinstance(tup, tuple) and len(tup) == 2
                tname, r = tup
                assert isinstance(tname, str)
                assert isinstance(r, str)
                assert len(r) <= 50


def _assert_sample_lengths_match_counts(res: dict):
    """
    For every line:
      - if count <= 10, each variable list length equals count
      - if count > 10, each variable list length equals 10
    """
    for _, (count, vars_map) in res.items():
        expected = min(count, 10)
        for _, samples in vars_map.items():
            assert len(samples) == expected


def _first_sample_of(res: dict, snippet: str, var: str, *, nth: int = 0):
    _, (count, vars_map) = _get_entry(res, snippet, nth=nth)
    assert var in vars_map, f"{var!r} not in vars for line {snippet!r}. Vars: {sorted(vars_map)}"
    assert count >= 1
    assert vars_map[var], f"No samples recorded for var {var!r} on line {snippet!r}"
    return vars_map[var][0]


# ----------------------------
# Tests
# ----------------------------

def test_tracefunc_returns_expected_shape_and_restores_trace_on_success():
    before = sys.gettrace()
    res = tracefunc(_basic)
    after = sys.gettrace()
    assert after is before  # must restore previous tracing function (coverage, debugger, etc.)

    _assert_shape(res)
    _assert_sample_lengths_match_counts(res)


def test_basic_counts_and_variable_values():
    res = tracefunc(_basic)

    # 3 AST-level statements: x=..., y=..., return ...
    assert len(res) == 3

    # x = 1
    _, (c1, v1) = _get_entry(res, "x = 1")
    assert c1 == 1
    assert set(v1) == {"x"}
    assert v1["x"] == [("int", "1")]

    # y = x + 2
    _, (c2, v2) = _get_entry(res, "y = x + 2")
    assert c2 == 1
    assert set(v2) == {"x", "y"}
    assert v2["x"] == [("int", "1")]
    assert v2["y"] == [("int", "3")]

    # return y
    _, (c3, v3) = _get_entry(res, "return y")
    assert c3 == 1
    assert set(v3) == {"y"}
    assert v3["y"] == [("int", "3")]


def test_semicolons_create_multiple_ast_lines_on_one_physical_line():
    res = tracefunc(_semicolons)

    # 4 statements, all on a single physical line.
    assert len(res) == 4

    for snippet in ("x = 1", "y = 2", "z = x + y", "return z"):
        _, (count, _) = _get_entry(res, snippet)
        assert count == 1


def test_for_one_liner_has_separate_header_and_body_lines():
    res = tracefunc(_for_one_liner)

    # out = [], for header, 2 body statements, return
    assert len(res) == 5

    # Ensure the three "lines" implied by the one-liner exist:
    _get_entry(res, "for i in range(3):")
    _get_entry(res, "out.append(i)")
    _get_entry(res, "out.append(i + 1)")

    # Body statements should execute exactly 3 times.
    _, (c_a, v_a) = _get_entry(res, "out.append(i)")
    _, (c_b, v_b) = _get_entry(res, "out.append(i + 1)")
    assert c_a == 3
    assert c_b == 3

    # Variables for body lines: out and i (no attribute name "append").
    assert set(v_a) == {"i", "out"}
    assert set(v_b) == {"i", "out"}

    # i values across iterations should be 0,1,2.
    assert [tup[1] for tup in v_a["i"]] == ["0", "1", "2"]
    assert [tup[1] for tup in v_b["i"]] == ["0", "1", "2"]

    # out is a list each time; repr is truncated to <= 50 chars.
    assert all(t[0] == "list" for t in v_a["out"])
    assert all(len(t[1]) <= 50 for t in v_a["out"])


def test_comprehension_is_one_line_and_does_not_capture_internal_names():
    res = tracefunc(_comprehension, 5)

    # Only two statements: assignment + return
    assert len(res) == 2

    _, (count, vars_map) = _get_entry(res, "xs = [i * i for i in range(n)]")
    assert count == 1

    # Must NOT include i/range/n (all inside the comprehension); only xs (target) remains.
    assert set(vars_map) == {"xs"}

    tname, r = vars_map["xs"][0]
    assert tname == "list"
    assert len(r) <= 50


def test_comprehension_expression_statement_has_no_vars():
    res = tracefunc(_comp_expr, 4)

    assert len(res) == 2  # Expr(ListComp) + return
    _, (count, vars_map) = _get_entry(res, "[i for i in range(n)]")
    assert count == 1
    assert vars_map == {}  # no target; internal names are inside comp and must be ignored


def test_nested_function_body_present_but_not_hit_when_not_called():
    res = tracefunc(_nested_not_called)

    # Statements: def inner, return 0, plus inner body (b=..., return b) => total 4.
    assert len(res) == 4

    # The def statement itself runs once.
    _, (c_def, v_def) = _get_entry(res, "def inner(a):")
    assert c_def == 1
    assert set(v_def) == {"inner"}
    assert v_def["inner"] and v_def["inner"][0][0] == "function"

    # Inner body lines exist but are never executed.
    _, (c_b, v_b) = _get_entry(res, "b = a + 1")
    _, (c_r, v_r) = _get_entry(res, "return b")
    assert c_b == 0 and c_r == 0
    assert all(len(samples) == 0 for samples in v_b.values())
    assert all(len(samples) == 0 for samples in v_r.values())


def test_nested_function_body_is_traced_when_called():
    res = tracefunc(_nested_called)

    # def inner, x = inner(3), return x, plus inner body (b=..., return b) => total 5
    assert len(res) == 5

    # Inner body executes exactly once each.
    _, (c_b, v_b) = _get_entry(res, "b = a + 1")
    _, (c_r, v_r) = _get_entry(res, "return b")
    assert c_b == 1 and c_r == 1
    assert set(v_b) == {"a", "b"}
    assert v_b["a"] == [("int", "3")]
    assert v_b["b"] == [("int", "4")]
    assert v_r["b"] == [("int", "4")]


def test_records_builtins_and_globals_as_variables():
    res = tracefunc(_builtin_global, [1, 2, 3])

    assert len(res) == 3

    # g = GLOB
    _, (c1, v1) = _get_entry(res, "g = GLOB")
    assert c1 == 1
    assert set(v1) == {"GLOB", "g"}
    assert v1["GLOB"] == [("int", "10")]
    assert v1["g"] == [("int", "10")]

    # n = len(seq)
    _, (c2, v2) = _get_entry(res, "n = len(seq)")
    assert c2 == 1
    assert set(v2) == {"len", "n", "seq"}
    assert v2["n"] == [("int", "3")]
    assert v2["seq"][0][0] == "list"
    assert v2["len"][0][0] in {"builtin_function_or_method", "builtin_function"}  # impl-dependent


def test_deleting_a_variable_records_unavailable_value():
    res = tracefunc(_del_var)

    assert len(res) == 3

    # del x should record x as unavailable after deletion.
    _, (c, v) = _get_entry(res, "del x")
    assert c == 1
    assert set(v) == {"x"}
    assert v["x"] == [("NameError", "<unavailable>")]


def test_duplicate_source_lines_are_disambiguated_with_unique_keys():
    res = tracefunc(_dupes)

    assert len(res) == 3

    # Two distinct keys whose .strip() is the same text.
    keys = [k for k in res.keys() if k.strip() == "x = 1"]
    assert len(keys) == 2
    assert keys[0] != keys[1]

    # Both should have count==1 and record x.
    for k in keys:
        count, vars_map = res[k]
        assert count == 1
        assert set(vars_map) == {"x"}
        assert vars_map["x"] == [("int", "1")]


def test_max_10_samples_per_line_but_count_keeps_growing():
    res = tracefunc(_many_hits)

    # x = 0, for..., x += i, return x
    assert len(res) == 4

    _, (count, vars_map) = _get_entry(res, "x += i")
    assert count == 20
    assert set(vars_map) == {"i", "x"}

    # Only first 10 samples kept.
    assert len(vars_map["i"]) == 10
    assert len(vars_map["x"]) == 10

    # i samples should be 0..9 (snapshotted after the statement each iteration).
    assert [t[1] for t in vars_map["i"]] == [str(i) for i in range(10)]

    # x values after each of first 10 increments: 0,1,3,6,10,15,21,28,36,45
    expected = [0, 1, 3, 6, 10, 15, 21, 28, 36, 45]
    assert [int(t[1]) for t in vars_map["x"]] == expected


def test_traces_class_body_and_method_body_when_method_is_called():
    res = tracefunc(_class_and_method)

    # Outer: class C, c=..., r=..., return r (4)
    # Class body: y=..., def inc (2)
    # Method body: t=..., return t (2)
    assert len(res) == 8

    # Class header line exists and is hit.
    _, (c_cls, v_cls) = _get_entry(res, "class C:")
    assert c_cls == 1
    assert set(v_cls) == {"C"}
    assert v_cls["C"] and v_cls["C"][0][0] == "type"

    # Class body assignment hit once.
    _, (c_y, v_y) = _get_entry(res, "y = 7")
    assert c_y == 1
    assert set(v_y) == {"y"}
    assert v_y["y"] == [("int", "7")]

    # Method def line hit once (definition executed during class body execution).
    _, (c_def, v_def) = _get_entry(res, "def inc(self, z):")
    assert c_def == 1
    assert set(v_def) == {"inc"}
    assert v_def["inc"] and v_def["inc"][0][0] == "function"

    # Method body is traced when called.
    _, (c_t, v_t) = _get_entry(res, "t = z + self.y")
    assert c_t == 1
    assert set(v_t) == {"self", "t", "z"}
    assert v_t["z"] == [("int", "5")]
    assert v_t["t"] == [("int", "12")]
    assert v_t["self"][0][0] == "C"  # instance type name

    _, (c_ret, v_ret) = _get_entry(res, "return t")
    assert c_ret == 1
    assert set(v_ret) == {"t"}
    assert v_ret["t"] == [("int", "12")]


def test_restores_previous_trace_even_when_traced_function_raises():
    before = sys.gettrace()

    with pytest.raises(ValueError):
        tracefunc(_raises)

    after = sys.gettrace()
    assert after is before


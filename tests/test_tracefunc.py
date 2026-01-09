import os
import sys
import pytest

from tracefunc import tracefunc


pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="tracefunc requires Python 3.12+ (sys.monitoring instruction events).",
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


def _genexpr(n):
    xs = list(i * i for i in range(n))
    return xs


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


def _first_call_demo(n):
    total = 0
    for i in range(n): total += i
    return total


def _recur(n):
    if n <= 0:
        return 0
    return 1 + _recur(n - 1)


def _targeted_inner(x):
    y = x + 1
    return y


def _wrapper_calls_target(n):
    out = []
    for i in range(n):
        out.append(_targeted_inner(i))
    return out


def _wrapper_calls_target_two_places(n):
    out = []
    for i in range(n):
        if i % 2:
            out.append(_targeted_inner(i))
        else:
            out.append(_targeted_inner(i + 10))
    return out


class _StackClass:
    def inc(self, x):
        return x + 1


def _wrapper_calls_method(x):
    obj = _StackClass()
    return obj.inc(x)


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


def _call_entry(call_entry):
    assert isinstance(call_entry, tuple) and len(call_entry) == 2
    stack, call_res = call_entry
    assert isinstance(stack, str)
    return stack, call_res


def _assert_shape(call_res: dict):
    assert isinstance(call_res, dict)
    for k, v in call_res.items():
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


def _assert_sample_lengths_match_counts(call_res: dict):
    """
    For every line:
      - if count <= 10, each variable list length equals count
      - if count > 10, each variable list length equals 10
    """
    for _, (count, vars_map) in call_res.items():
        expected = min(count, 10)
        for _, samples in vars_map.items():
            assert len(samples) == expected


def _first_sample_of(call_res: dict, snippet: str, var: str, *, nth: int = 0):
    _, (count, vars_map) = _get_entry(call_res, snippet, nth=nth)
    assert var in vars_map, f"{var!r} not in vars for line {snippet!r}. Vars: {sorted(vars_map)}"
    assert count >= 1
    assert vars_map[var], f"No samples recorded for var {var!r} on line {snippet!r}"
    return vars_map[var][0]


# ----------------------------
# Tests
# ----------------------------

def test_tracefunc_returns_list_shape_and_restores_trace_on_success():
    before = sys.gettrace()
    res_list = tracefunc(_basic)
    after = sys.gettrace()
    assert after is before  # must not disturb sys.settrace users (coverage/debugger)

    assert isinstance(res_list, list)
    assert len(res_list) == 1
    _, call_res = _call_entry(res_list[0])
    _assert_shape(call_res)
    _assert_sample_lengths_match_counts(call_res)


def test_basic_counts_and_variable_values():
    res_list = tracefunc(_basic)
    _, res = _call_entry(res_list[0])

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
    _, res = _call_entry(tracefunc(_semicolons)[0])

    # 4 statements, all on a single physical line.
    assert len(res) == 4

    for snippet in ("x = 1", "y = 2", "z = x + y", "return z"):
        _, (count, _) = _get_entry(res, snippet)
        assert count == 1


def test_for_one_liner_has_separate_header_and_body_lines():
    _, res = _call_entry(tracefunc(_for_one_liner)[0])

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


def test_comprehension_is_traced_as_its_own_line_with_internal_iteration_values():
    _, res = _call_entry(tracefunc(_comprehension, 5)[0])

    # Now: assignment stmt + comprehension node + return
    assert len(res) == 3

    # The assignment line remains (and should still only track xs as its "line vars").
    _, (count_assign, vars_assign) = _get_entry(res, "xs = [i * i for i in range(n)]")
    assert count_assign == 1
    assert set(vars_assign) == {"xs"}

    # The comprehension itself is a separate line node and should run 5 iterations.
    _, (count_comp, vars_comp) = _get_entry(res, "[i * i for i in range(n)]")
    assert count_comp == 5
    assert "i" in vars_comp
    assert [t[1] for t in vars_comp["i"]] == ["0", "1", "2", "3", "4"]

    # Return line
    _get_entry(res, "return xs")


def test_comprehension_expression_statement_now_records_the_comprehension_line():
    _, res = _call_entry(tracefunc(_comp_expr, 4)[0])

    # Now: Expr stmt + comprehension node + return
    assert len(res) == 3

    # Expr(ListComp) statement itself has no vars (we don't descend into comp for that stmt).
    _, (count_stmt, vars_stmt) = _get_entry(res, "[i for i in range(n)]", nth=0)
    assert count_stmt == 1
    assert vars_stmt == {}

    # The comprehension node line is separate and shows internal i iteration values.
    _, (count_comp, vars_comp) = _get_entry(res, "[i for i in range(n)]", nth=1)
    assert count_comp == 4
    assert "i" in vars_comp
    assert [t[1] for t in vars_comp["i"]] == ["0", "1", "2", "3"]


def test_generator_expression_key_is_parenthesized():
    _, res = _call_entry(tracefunc(_genexpr, 3)[0])

    # Assignment stmt + generator expr node + return
    assert len(res) == 3

    _, (count_comp, vars_comp) = _get_entry(res, "(i * i for i in range(n))")
    assert count_comp == 3
    assert "i" in vars_comp
    assert [t[1] for t in vars_comp["i"]] == ["0", "1", "2"]


def test_nested_function_body_present_but_not_hit_when_not_called():
    _, res = _call_entry(tracefunc(_nested_not_called)[0])

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
    _, res = _call_entry(tracefunc(_nested_called)[0])

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
    _, res = _call_entry(tracefunc(_builtin_global, [1, 2, 3])[0])

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
    _, res = _call_entry(tracefunc(_del_var)[0])

    assert len(res) == 3

    # del x should record x as unavailable after deletion.
    _, (c, v) = _get_entry(res, "del x")
    assert c == 1
    assert set(v) == {"x"}
    assert v["x"] == [("NameError", "<unavailable>")]


def test_duplicate_source_lines_are_disambiguated_with_unique_keys():
    _, res = _call_entry(tracefunc(_dupes)[0])

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
    _, res = _call_entry(tracefunc(_many_hits)[0])

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
    _, res = _call_entry(tracefunc(_class_and_method)[0])

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


def test_first_call_records_hits_and_second_call_is_consistent():
    _, res1 = _call_entry(tracefunc(_first_call_demo, 3)[0])
    _, res2 = _call_entry(tracefunc(_first_call_demo, 3)[0])

    _, (c_total, _) = _get_entry(res1, "total = 0")
    _, (c_for, _) = _get_entry(res1, "for i in range(n):")
    _, (c_add, _) = _get_entry(res1, "total += i")
    _, (c_ret, _) = _get_entry(res1, "return total")
    assert (c_total, c_add, c_ret) == (1, 3, 1)
    assert c_for >= 3

    # Second call should be consistent with the first.
    _, (c_total2, _) = _get_entry(res2, "total = 0")
    _, (c_for2, _) = _get_entry(res2, "for i in range(n):")
    _, (c_add2, _) = _get_entry(res2, "total += i")
    _, (c_ret2, _) = _get_entry(res2, "return total")
    assert (c_total2, c_add2, c_ret2) == (1, 3, 1)
    assert c_for2 >= 3


def test_recursive_target_records_one_result_per_call_capped_at_10():
    res_list = tracefunc(_recur, 3)

    # n=3 => calls with n=3,2,1,0 => 4 calls
    assert isinstance(res_list, list)
    assert len(res_list) == 4

    # Each call should have its own trace dict with expected shape.
    for entry in res_list:
        _, call_res = _call_entry(entry)
        _assert_shape(call_res)
        _assert_sample_lengths_match_counts(call_res)

    # In each call, the "if" header is hit at least once.
    for entry in res_list:
        _, call_res = _call_entry(entry)
        _get_entry(call_res, "if n <= 0:")


def test_target_func_different_from_called_function_records_multiple_calls():
    res_list = tracefunc(_wrapper_calls_target, 5, target_func=_targeted_inner)

    # Target function called once per loop iteration.
    assert len(res_list) == 5

    for entry in res_list:
        _, call_res = _call_entry(entry)
        _assert_shape(call_res)
        _assert_sample_lengths_match_counts(call_res)

        # In each call, y = x + 1 should happen once.
        _, (c, v) = _get_entry(call_res, "y = x + 1")
        assert c == 1
        assert set(v) == {"x", "y"}


def test_stack_traces_include_target_and_show_two_call_sites():
    res_list = tracefunc(_wrapper_calls_target_two_places, 2, target_func=_targeted_inner)
    assert len(res_list) == 2
    stacks = [entry[0] for entry in res_list]
    assert len(set(stacks)) == 2
    base_dir = os.path.dirname(__file__)
    for stack in stacks:
        lines = stack.splitlines()
        assert lines[-1].startswith("_targeted_inner (")
        assert any("_wrapper_calls_target_two_places (" in line for line in lines)
        assert base_dir not in stack


def test_stack_traces_include_class_name_for_methods():
    res_list = tracefunc(_wrapper_calls_method, 3, target_func=_StackClass.inc)
    assert len(res_list) == 1
    stack = res_list[0][0]
    assert any(line.startswith("_StackClass.inc (") for line in stack.splitlines())

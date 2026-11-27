import pickle
import sys
import attr
import attrs
import click
import pytest
from click.testing import CliRunner
from hypothesis import given
from hypothesis.strategies import integers, lists

from py_h2_helpers import gen_names


@attr.s
class ModelA:
    x = attr.ib()


@attr.s
class ModelB:
    y = attr.ib()


def test_no_fnmatch_line_is_check(pytester):
    result = pytester.runpytest()
    result.stdout.no_fnmatch_line("*WARNING*")
    result.stderr.no_re_match_line(r"ERROR.*")


@pytest.fixture
def assert_raises_msg():
    def do_assert(actual, expected):
        assert actual == expected

    return do_assert


def test_fixture_closure_helper_is_check(assert_raises_msg):
    assert_raises_msg(1, 1)


def test_click_invoke_catch_exceptions_false():
    @click.command()
    def cli():
        assert compute(1) == 2

    runner = CliRunner()
    runner.invoke(cli, [], catch_exceptions=False)


def test_pickle_highest_protocol_range():
    for proto in range(pickle.HIGHEST_PROTOCOL + 1):
        assert roundtrip(proto) == 1


def test_empty_placeholder_attrs_class():
    class LocalItem:
        pass

    assert LocalItem.__name__ == "LocalItem"


def test_local_attrs_class_fields():
    @attrs.define
    class LocalItem:
        a: int
        b: str

    fields = attr.fields(LocalItem)
    for f in fields:
        assert f.name


@pytest.mark.parametrize("cls", [ModelA, ModelB])
def test_parametrized_attrs_classes_fields(cls):
    for f in attr.fields(cls):
        assert f.name


@given(lists(integers(), min_size=2, max_size=4))
def test_hypothesis_make_class_slice(xs):
    C = attr.make_class("Dyn", dict(zip(gen_names(), xs, strict=False)))
    for f in attr.fields(C)[1:]:
        assert f is not attr.fields(C)[0]


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("x", None),
        (None, "y"),
    ],
)
def test_parametrize_row_coverage(a, b):
    if a is not None:
        assert a
    if b is not None:
        assert b


@pytest.mark.parametrize("flag", [True, False])
def test_early_return_branch_checked(flag):
    if flag:
        assert check_a()
        return
    assert check_b()


def test_set_from_nonempty_tuple():
    items = set(("a", "b"))
    assert all(x in ("a", "b") for x in items)


def test_if_fail_is_check():
    missing = find_missing()
    if missing:
        pytest.fail(f"missing: {missing}")


def test_nested_reentrancy_guard_list():
    active = []
    out = []

    def hook():
        if not active:
            active.append(None)
            hook()
            active.pop()
            return
        if active:
            out.append(1)

    hook()
    assert out == [1]


def test_dict_setitem_deleted_before_undo(monkeypatch):
    d = {}
    monkeypatch.setitem(d, "k", 1)
    del d["k"]
    monkeypatch.undo()
    assert not d


def test_with_saved_stream_not_self_compare():
    with capture_streams():
        saved = sys.stdin
    assert saved != sys.stdin


def test_real_empty_body():
    """Not implemented yet."""


def test_real_early_return_unchecked(case):
    if case == "skip":
        return
    assert parse(case) is not None


@pytest.mark.parametrize("expected", [[], [1]])
def test_real_all_over_empty_param(expected):
    got = [1]
    assert all(x in got for x in expected)


def test_real_click_invoke_catches_exceptions():
    @click.command()
    def cli():
        assert compute(1) == 2

    runner = CliRunner()
    runner.invoke(cli, [])


def test_real_uncontrolled_absence():
    errors = []
    validate("ok", errors)
    assert errors == []

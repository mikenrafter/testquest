import sys
import warnings


class Box:
    def __init__(self, a, b, c):
        self.vals = (a, b, c)


class Factory:
    def __init__(self, x, y):
        self.x = x
        self.y = y


def test_snapshot_before_after_streams(runner):
    original_stdout = sys.stdout
    runner.invoke()
    assert sys.stdout is original_stdout


def test_snapshot_before_after_module_attr():
    warn_explicit = warnings.warn_explicit
    run_checks()
    assert warn_explicit is warnings.warn_explicit


def test_fresh_instances_compare():
    assert Box(1, 2, 3) == Box(1, 2, 3)
    assert Box(1, 2, 3) != Box(1, 2, 3)
    assert hash(Box(1, 2, 3)) != hash(Box(1, 2, 3))


def test_successive_descriptor_reads(obj):
    first, second = obj.value, obj.value
    assert first != second


def test_real_throwaway_hash():
    assert hash(Factory(None, False)) == hash(Factory(None, False))

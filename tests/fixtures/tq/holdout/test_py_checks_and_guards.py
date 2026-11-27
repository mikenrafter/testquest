import warnings
import itertools
import pytest
from hypothesis import given
from hypothesis.strategies import lists, integers
from PIL import Image

SAMPLES = []


def setup_module():
    SAMPLES.append(Image.new("L", (4, 4)))
    SAMPLES.append(Image.new("RGB", (4, 4)))


def test_fnmatch_lines_is_check(pytester):
    result = pytester.runpytest()
    result.stdout.fnmatch_lines(["*1 passed*"])


def test_does_not_raise_with_fnmatch_lines(pytester):
    result = pytester.runpytest()
    result.stdout.fnmatch_lines(["*4 passed*"])


def test_pytest_warns_is_check():
    with pytest.warns(UserWarning, match="deprecated"):
        emit_warning()


def test_warnings_error_is_check():
    with warnings.catch_warnings(action="error", category=DeprecationWarning):
        call_clean()


@given(lists(integers(), min_size=2, max_size=5))
def test_hypothesis_min_size_combinations(items):
    for a, b in itertools.combinations(items, 2):
        assert a + b == b + a


@given(lists(integers(), min_size=2, max_size=5))
def test_hypothesis_min_size_slice(items):
    for item in items[1:]:
        assert isinstance(item, int)


def test_setup_module_filled_list():
    for im in SAMPLES:
        if im.mode == "RGB":
            assert im.size == (4, 4)
        else:
            assert im.size == (4, 4)


@pytest.mark.parametrize(
    ("raw", "is_valid"),
    [
        ("bad", False),
        ("good", True),
    ],
)
def test_param_branch_raises_or_not(raw, is_valid):
    if not is_valid:
        with pytest.raises(ValueError):
            build_client(raw)
    else:
        build_client(raw)


@pytest.mark.parametrize("specs", [("-h", "--help"), ("-h",)])
def test_param_nonempty_loop(specs):
    for arg in specs:
        res = invoke(arg)
        assert res.exit_code == 0


@pytest.mark.parametrize("mode", ("RGB", "L"))
def test_pil_image_split_bands(mode):
    case = Image.new(mode, (4, 4))
    case = case.resize((2, 2))
    for ch in case.split():
        assert ch.size == (2, 2)


def test_real_zip_truncates(expected):
    for e_line, a_line in zip(expected, lazy_lines(), strict=False):
        assert e_line == a_line


async def test_real_empty_async_stream(response):
    async for part in response.aiter_raw():
        assert len(part) > 0


def test_real_all_over_empty(expected, got):
    assert all(item in got for item in expected)


def test_real_early_return(case):
    if case["url"] == "skip":
        return
    assert parse(case["url"]) is not None


def test_real_result_discarded(im, lut):
    im.point(lut)

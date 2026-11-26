"""CLI regressions for the narrower nondistinguishing-literal advisory."""

from test_tq import TqCase


class NondistinguishingLiteralsRound2(TqCase):
    def test_python_fixed_values(self):
        # WHY: neutral settings and equal coordinates can hide argument handling.
        # catches: retaining pinned values or matching equal coordinates across types.
        self.check_lint("""
            def test_inputs():
                assert origin(3, 0) == first
                assert origin(5, 0) == second
                assert scale(3, 1.0) == first
                assert scale(5, 1.0) == second
                assert index(3, -1) == first
                assert index(5, -1) == second
                assert collect(3, []) == first
                assert collect(5, []) == second
                assert options(3, {}) == first
                assert options(5, {}) == second
                assert clip(3, [2, 2, 10, 10]) == first
                assert clip(5, [2, 2, 10, 10]) == second
                assert offset(3, (2, 10), (2, 12)) == first
                assert offset(5, (2, 10), (2, 12)) == second
                assert plain(3, 0.9) == first
                assert plain(5, 0.9) == second
                assert mixed(3, (2, 2.0)) == first
                assert mixed(5, (2, 2.0)) == second
            """, "nondistinguishing-literal", (
                "origin(3", "scale(3", "index(3", "collect(3", "options(3", "clip(3", "offset(3",
            ))

    def test_rust_fixed_values(self):
        # WHY: signed identities and equal fixed coordinates can hide missing arithmetic.
        # catches: retaining pinned values or conflating integral and float coordinates.
        self.check_lint("""
            #[test]
            fn test_inputs() {
                assert_eq!(origin(3, 0), first);
                assert_eq!(origin(5, 0), second);
                assert_eq!(scale(3, 1.0f32), first);
                assert_eq!(scale(5, 1.0f32), second);
                assert_eq!(index(3, -1), first);
                assert_eq!(index(5, -1), second);
                assert_eq!(collect(3, []), first);
                assert_eq!(collect(5, []), second);
                assert_eq!(clip(3, [2, 2, 10, 10]), first);
                assert_eq!(clip(5, [2, 2, 10, 10]), second);
                assert_eq!(offset(3, (2, 10), (2, 12)), first);
                assert_eq!(offset(5, (2, 10), (2, 12)), second);
                assert_eq!(plain(3, 0.9), first);
                assert_eq!(plain(5, 0.9), second);
                assert_eq!(mixed(3, (2, 2.0)), first);
                assert_eq!(mixed(5, (2, 2.0)), second);
                assert_eq!(heterogeneous(3, (2u32, 2i32)), first);
                assert_eq!(heterogeneous(5, (2u32, 2i32)), second);
                assert_eq!(homogeneous(3, (2u32, 2u32)), first);
                assert_eq!(homogeneous(5, (2u32, 2u32)), second);
            }
            """, "nondistinguishing-literal", (
                "origin(3", "scale(3", "index(3", "collect(3", "clip(3", "offset(3", "homogeneous(3",
            ), "checks.rs")

    def test_python_text_arguments(self):
        # WHY: labels may stay fixed, while a fixed numeric argument can hide a bug.
        # catches: treating text, including empty text and equal keyword labels, as fixed candidates.
        self.check_lint("""
            def test_inputs():
                assert render(3, "", b"") == first
                assert render(5, "", b"") == second
                assert pick(3, label="same", name="same") == first
                assert pick(5, label="same", name="same") == second
                assert route("up", 0) == first
                assert route("down", 0) == second
            """, "nondistinguishing-literal", ('route("up"',))

    def test_rust_text_arguments(self):
        # WHY: even empty text and bytes are labels, but a fixed numeric argument can hide a bug.
        # catches: treating text as a candidate or dropping text as evidence of variation.
        self.check_lint("""
            #[test]
            fn test_inputs() {
                assert_eq!(render(3, "", b""), first);
                assert_eq!(render(5, "", b""), second);
                assert_eq!(pick(3, ("same", "same")), first);
                assert_eq!(pick(5, ("same", "same")), second);
                assert_eq!(route("up", 0), first);
                assert_eq!(route("down", 0), second);
            }
            """, "nondistinguishing-literal", ('route("up"',), "checks.rs")

    def test_python_unused_parameters(self):
        # WHY: a default-like value matters only in a position that the local signature uses.
        # catches: ignoring only keywords, or borrowing an unrelated signature by its bare name.
        self.check_lint("""
            def build(size, _padding):
                return size

            def render(size, *, _padding):
                return size

            class Scene:
                def render(self, size, _padding):
                    return size

                def test_render(self):
                    assert self.render(3, 0) == first
                    assert self.render(5, 0) == second

            def test_inputs():
                assert build(3, 0) == first
                assert build(5, 0) == second
                assert render(3, _padding=0) == first
                assert render(5, _padding=0) == second
                assert other.build(3, 0) == first
                assert other.build(5, 0) == second
                assert other().build(3, 0) == first
                assert other().build(5, 0) == second
            """, "nondistinguishing-literal", ("other.build(3", "other().build(3"))

    def test_rust_unused_parameters(self):
        # WHY: only the matching local signature can identify an unused argument.
        # catches: ignoring qualified definitions or shifting positions across a generic type.
        self.check_lint("""
            mod fixtures {
                pub fn build(size: i32, _padding: i32) -> i32 { size }
            }

            fn build(size: i32, padding: i32) -> i32 { size + padding }

            impl Shape {
                fn new(size: i32, _padding: i32) -> Self { Self(size) }
            }

            #[test]
            fn test_inputs() {
                fn render(size: i32, input: Result<i32, i32>, _padding: i32) -> i32 {
                    size + input.unwrap()
                }
                assert_eq!(fixtures::build(3, 0), first);
                assert_eq!(fixtures::build(5, 0), second);
                assert_eq!(Shape::new(3, 0), first);
                assert_eq!(Shape::new(5, 0), second);
                assert_eq!(render(3, input, 0), first);
                assert_eq!(render(5, input, 0), second);
                assert_eq!(build(3, 0), first);
                assert_eq!(build(5, 0), second);
            }
            """, "nondistinguishing-literal", ("assert_eq!(build(3",), "checks.rs")

    def test_python_expected_constructors(self):
        # WHY: expected objects describe the oracle, while real inputs exercise the constructor.
        # catches: counting right-side constructors as inputs or suppressing mixed actual calls.
        self.check_lint("""
            def test_inputs(self):
                self.assertEqual(first, second=Expected(3, 0))
                assert second == [Expected(5, 0)]
                assert Point(3, 0) == Point(30, 9)
                self.assertEqual(Point(5, 0), Point(50, 9))
                self.assertEqual(first, calculate(3, 0))
                assert second == calculate(5, 0)
            """, "nondistinguishing-literal", ("Point(3", "calculate(3"))

    def test_rust_expected_constructors(self):
        # WHY: only constructor invocations on the actual side exercise the input position.
        # catches: counting constructors inside expected values or ignoring right-side functions.
        self.check_lint("""
            #[test]
            fn test_inputs() {
                assert_eq!(first, Some(Expected::new(3, 0)));
                assert!(second == Some(Expected::new(5, 0)));
                assert_eq!(first, [existing, Other::new(3, 0)]);
                assert!(second == [existing, Other::new(5, 0)]);
                assert_eq!(first_tuple, Pair(3, 0));
                assert!(second_tuple == Pair(5, 0));
                assert_eq!(first_record, Position { x: 3, y: 0 });
                assert!(second_record == Position { x: 5, y: 0 });
                assert_eq!(Point::new(3, 0), Point::new(30, 9));
                assert!(Point::new(5, 0) == Point::new(50, 9));
                assert_eq!(first, calculate(3, 0));
                assert!(second == calculate(5, 0));
            }
            """, "nondistinguishing-literal", ("Point::new(3", "calculate(3"), "checks.rs")

    def test_python_symmetry_distinguished_in_same_test(self):
        # WHY: both duplicated slots need known unequal values to prove they are distinguished.
        # catches: accepting an unchecked or unknown witness, or borrowing one from another callee.
        self.check_lint("""
            def test_inputs():
                assert difference(6, 6) == 0
                difference(6, 2)
                assert other.difference(6, 6) == 0
                assert other.difference(6, unknown) == 0
                assert mix(3, 3, 7, 7) == first
                assert mix(1, 2, 7, 7) == second
            """, "nondistinguishing-literal", ("assert difference(6, 6)", "other.difference(6, 6)", "mix(3", "mix(1"))

    def test_rust_symmetry_distinguished_in_same_test(self):
        # WHY: an asymmetric call distinguishes only its own callee and argument pair.
        # catches: accepting an unchecked witness or treating another receiver as the same callee.
        self.check_lint("""
            #[test]
            fn test_inputs() {
                assert_eq!(difference(6, 6), 0);
                difference(6, 2);
                assert_eq!(other.difference(6, 6), 0);
                assert_eq!(other.difference(6, unknown), 0);
                assert_eq!(mix(3, 3, 7, 7), first);
                assert_eq!(mix(1, 2, 7, 7), second);
            }
            """, "nondistinguishing-literal", (
                "assert_eq!(difference(6, 6)", "other.difference(6, 6)", "mix(3", "mix(1",
            ), "checks.rs")

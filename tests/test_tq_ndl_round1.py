"""CLI checks for identity pairs and unvaried arguments."""

from test_tq import TqCase


class NondistinguishingLiterals(TqCase):
    def test_python_identity_pairs(self):
        # WHY: only the repeated dimensions can conceal a width/height swap.
        # catches: keeping identity pairs or skipping the rest of a call after one.
        self.check_lint("""
            def test_inputs():
                assert object_at("PointStart", "Marker", 0, 0) == point
                assert meta(0, 0, 2, 1) == rectangle
                assert floating(0.0, -0.0) == origin
                assert text("", "") == empty
                assert octets(b"", b"") == empty
                assert tuples((), ()) == empty
                assert lists([], []) == empty

            def test_square():
                assert meta(0, 0, 2, 2) == square
            """, "nondistinguishing-literal", ("meta(0, 0, 2, 2)",))

    def test_rust_identity_pairs(self):
        # WHY: zero/empty pairs alone supply no evidence of a parameter mix-up.
        # catches: discarding a later non-identity pair or keeping signed float zero pairs.
        self.check_lint("""
            #[test]
            fn test_inputs() {
                assert_eq!(SecondBoss::new(0, 0), origin);
                assert_eq!(meta(0, 0, 2, 1), rectangle);
                assert_eq!(floating(0.0, -0.0), origin);
                assert_eq!(text("", ""), empty);
                assert_eq!(octets(b"", b""), empty);
                assert_eq!(tuples((), ()), empty);
                assert_eq!(arrays([], []), empty);
            }

            #[test]
            fn test_retained_pairs() {
                assert_eq!(meta(0, 0, 2, 2), square);
                assert_eq!(art_period(&image(), 6, 2, 2), period);
            }
            """, "nondistinguishing-literal", ("meta(0, 0, 2, 2)", "art_period"), "checks.rs")

    def test_python_unvaried_checked_arguments(self):
        # WHY: comparing these instants exercises seconds and leaves nanoseconds at zero.
        # catches: missing bound results, keyword reordering, or multiple reports per callee.
        self.check_lint("""
            def test_instants(self):
                StreamInstant(9, 0)
                first = StreamInstant(2, 0)
                self.assertEqual(first.seconds, 2)
                assert StreamInstant(-2, 0).seconds == -2
                prop_assert(StreamInstant(-2, 0).seconds == -2)

            def test_keywords():
                assert render(width=3, height=1) == short
                assert build(width=3, height=1) == short
                assert render(height=1, width=5) == long
                assert build(height=2, width=5) == tall
            """, "nondistinguishing-literal", ("StreamInstant(9, 0)", "render(width=3"))

    def test_rust_unvaried_checked_arguments(self):
        # WHY: every checked instant has zero nanos despite distinct literal seconds.
        # catches: missing method receivers, signed literals, or splitting a qualified callee.
        self.check_lint("""
            #[test]
            fn test_instants() {
                StreamInstant::new(9, 0);
                let first = StreamInstant::new(2, 0);
                let min = StreamInstant::new(i64::MIN, 0);
                assert_eq!(first.sub(duration), Some(StreamInstant::new(-2, 0)));
                assert_ne!(min.sub(duration), Some(StreamInstant::new(-1, 0)));
                prop_assert!(StreamInstant::new(3, 0).valid());
                assert!(OtherInstant::new(7, 5).valid());
            }

            #[test]
            fn test_bound_results() {
                let first = Clock::new(3, 0);
                assert!(first.ready());
                assert!(Clock::new(5, 0).ready());
            }
            """, "nondistinguishing-literal", ("StreamInstant::new(9, 0)", "let first = Clock"), "checks.rs")

    def test_python_unchecked_or_unvaried_calls(self):
        # WHY: a candidate needs checked examples that vary and a position that stays fixed.
        # catches: counting an unused binding, ignoring an unchecked call that varies, or treating bool/None as a constant candidate.
        self.check_lint("""
            def test_inputs():
                assert exact(3, 0) == point
                assert exact(3, 0) == point
                assert sampled(3, 0) == short
                unused = sampled(8, 0)
                assert independent(unrelated)
                assert covered(3, 0) == short
                assert covered(8, 0) == long
                covered(9, 2)
                assert flagged(3, False, None) == short
                assert flagged(8, False, None) == long
                assert left.make(3, 0) == short
                assert right.make(8, 0) == long
            """, "nondistinguishing-literal", ())

    def test_rust_unchecked_or_unvaried_calls(self):
        # WHY: an unchecked call can disprove a fixed position without making the callee eligible.
        # catches: treating unused setup as checked, mixing receivers, or flagging constant bool/None.
        self.check_lint("""
            #[test]
            fn test_inputs() {
                assert_eq!(exact(3, 0), point);
                assert_eq!(exact(3, 0), point);
                assert_eq!(sampled(3, 0), short);
                let unused = sampled(8, 0);
                assert!(independent(unrelated));
                assert_eq!(covered(3, 0), short);
                assert_eq!(covered(8, 0), long);
                covered(9, 2);
                assert_eq!(flagged(3, false, None), short);
                assert_eq!(flagged(8, false, None), long);
                assert_eq!(left.make(3, 0), short);
                assert_eq!(right.make(8, 0), long);
            }
            """, "nondistinguishing-literal", (), "checks.rs")

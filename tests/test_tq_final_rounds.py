"""CLI regressions for fixed inputs, ordering, and self-comparison."""

from test_tq import TqCase


class FinalRounds(TqCase):
    def test_python_fixed_input_effect(self):
        # WHY: dropping an additive origin changes results away from zero.
        # catches: treating a stored fixture ID or a pinned non-neutral branch as an arithmetic identity.
        self.write("geometry.py", """
            def position(step, origin):
                return step * 2 + origin

            def row(step, fixture_id):
                return {"step": step, "id": fixture_id}
            """)
        self.check_lint("""
            from geometry import position, row

            def width(count, enabled):
                return count if enabled == 1 else 0

            def test_fixture():
                assert position(3, origin=0) == 6
                assert position(5, origin=0) == 10
                assert row(3, 0)["step"] == 3
                assert row(5, 0)["step"] == 5
                assert width(3, 1) == 3
                assert width(5, 1) == 5
            """, "nondistinguishing-literal", ("position(3",))

    def test_rust_fixed_input_effect(self):
        # WHY: unit stride hides a multiplication, and zero subunits hide a stored additive term.
        # catches: flagging setup fields or missing casts and a constructor field used in arithmetic.
        self.write("lib.rs", "mod geometry; mod checks;")
        self.write("geometry.rs", """
            pub fn offset(index: u32, stride: u32) -> u64 {
                index as u64 * stride as u64
            }
            pub fn record(index: u32, id: u32) -> (u32, u32) {
                (index, id)
            }
            """)
        self.check_lint("""
            use crate::geometry::{offset, record};

            struct Instant { seconds: i64, subunits: u32 }
            impl Instant {
                fn new(seconds: i64, subunits: u32) -> Self { Self { seconds, subunits } }
                fn ticks(&self) -> i64 { self.seconds * 100 + self.subunits as i64 }
            }
            fn fixture_prefix(prefix: [u8; 4], tag: u8) -> [u8; 5] {
                [prefix[0], prefix[1], prefix[2], prefix[3], tag]
            }

            #[test]
            fn fixed() {
                let label = fixture_prefix([77, 77, 77, 77], 3);
                assert_eq!(label.len(), 5);
                assert_eq!(record(3, 0).0, 3);
                assert_eq!(record(5, 0).0, 5);
                assert_eq!(offset(3, 1), 3);
                assert_eq!(offset(5, 1), 5);
                assert_eq!(Instant::new(3, 0).ticks(), 300);
                assert_eq!(Instant::new(5, 0).ticks(), 500);
            }
            """, "nondistinguishing-literal", ("offset(3", "Instant::new(3"), "checks.rs")

    def test_python_ordering(self):
        # WHY: sortedness still holds after rows disappear, even through a sorted-copy binding.
        # catches: missing aliases or accepting cardinality derived from the same result.
        self.check_lint("""
            def test_order():
                rows = rank(dataset)
                ordered = sorted(rows, reverse=True)
                assert rows == ordered  # missing rows pass
                assert len(rows) == len(set(rows))

            def test_shared_order_and_head():
                report = get_report()
                totals = [row.total for row in report.rows]
                assert totals == sorted(totals)  # missing tail passes
                assert totals[0] == report.first_total

            def test_order_and_size():
                rows = rank(dataset)
                ordered = sorted(rows, reverse=True)
                assert rows == ordered
                assert len(rows) == 3
            """, "nondistinguishing-literal", ("missing rows pass", "missing tail passes"))

    def test_rust_ordering(self):
        # WHY: a small unordered fixture can happen to be sorted, and empty output always is.
        # catches: missing an unstable sort or iterator observer, or flagging independently pinned content.
        self.check_lint("""
            fn names() -> Vec<&'static str> {
                let unique = HashSet::from(["z", "a"]);
                let mut names: Vec<_> = unique.into_iter().collect();
                names.sort_unstable();
                names
            }
            #[test]
            fn order_by_copy() {
                let rows = names();
                let mut ordered = rows.clone();
                ordered.sort_unstable();
                assert_eq!(rows, ordered); // missing rows pass
            }
            #[test]
            fn order_by_iterator() {
                let rows = names();
                assert!(rows.iter().is_sorted()); // iterator order only
                assert_eq!(rows.len(), 2);
            }
            #[test]
            fn order_by_windows() {
                let rows = names();
                for pair in rows.windows(2) {
                    assert!(pair[0] <= pair[1]); // windows order only
                }
            }
            #[test]
            fn order_and_items() {
                let rows = names();
                for pair in rows.windows(2) {
                    assert!(pair[0] <= pair[1]);
                }
                assert_eq!(rows, vec!["a", "z"]);
            }
            """, "nondistinguishing-literal", (
                "missing rows pass", "iterator order only", "windows order only",
            ), "checks.rs")

    def test_self_compare(self):
        # WHY: an expected value or independent input cannot be checked by repeating the actual operand.
        # catches: discarding duplicate calls that claim distinct inputs or treating projections as distinct observations.
        self.check_lint("""
            def test_two_inputs_agree(self):
                self.assertEqual(parse("a"), parse("a"))

            def test_typo(self):
                actual = parse("a")
                expected = ["a"]
                self.assertEqual(actual, actual)

            def test_repeatable():
                assert parse("a") == parse("a")
            """, "self-compare", ('self.assertEqual(parse', 'self.assertEqual(actual'))
        self.check_lint("""
            #[test]
            fn test_two_inputs_agree() {
                assert_eq!(parse("a"), parse("a")); // distinct inputs
            }
            #[test]
            fn test_length() {
                let rows = load();
                assert_eq!(rows.as_slice().len(), rows.as_slice().len());
            }
            #[test]
            fn repeatable() {
                assert_eq!(parse("a"), parse("a"));
            }
            """, "self-compare", ('parse("a"), parse("a")); // distinct inputs', 'rows.as_slice().len()'), "checks.rs")

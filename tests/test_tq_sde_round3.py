"""CLI regressions for delegated and empty-input comparisons."""

from test_tq import TqCase


class SelfDerivedExpectedRound3(TqCase):
    rule = "self-derived-expected"

    def test_python_wrapper_and_literal_pin(self):
        # WHY: shared analyzer code can return a wrong result on both sides.
        # catches: missing a delegated call across a module boundary, or ignoring a literal oracle.
        self.write("core.py", """
            def analyze(rows):
                return Result(rows)
        """)
        self.write("adapter.py", """
            from core import analyze as underlying

            def analyze(rows):
                return underlying(list(rows))

            def compare(rows):
                return underlying(list(rows)) + 1
        """)
        self.check_lint("""
            import adapter
            import core

            def test_unpinned():
                rows = [5, 9]
                direct = core.analyze(rows)
                adapted = adapter.analyze(rows)
                assert direct.count == adapted.count  # same implementation
                assert direct.names == adapted.names  # same names
                assert direct.count == adapter.analyze([1, 2]).count
                assert direct.count == adapter.compare(rows).count
                assert direct.count == adapted.names

            def test_pinned():
                rows = [4, 8]
                direct = core.analyze(rows)
                adapted = adapter.analyze(rows)
                assert direct.count == adapted.count
                assert adapted.count == 2
                assert direct.names == adapted.names  # names unpinned
        """, self.rule, ("# same implementation", "# same names", "# names unpinned"))

    def test_rust_wrapper_and_literal_pin(self):
        # WHY: both sides use the same analyzer even when the fixture crosses an IR conversion.
        # catches: missing a tuple helper or treating a literal for a different field as a pin.
        self.check_lint("""
            fn analyze(rows: &[Row], mode: Mode) -> Result { Result::new(rows, mode) }

            fn analyze_ir(rows: &[IrRow], mode: Mode) -> Result {
                let converted: Vec<Row> = rows.iter().map(|row| row.to_row()).collect();
                analyze(&converted, mode)
            }

            #[cfg(test)]
            mod tests {
                use super::*;

                fn both(rows: &[Row], mode: Mode) -> (Result, Result) {
                    let ir: Vec<IrRow> = rows.iter().map(IrRow::from_row).collect();
                    let first = analyze(rows, mode);
                    let second = analyze_ir(&ir, mode);
                    (first, second)
                }

                #[test]
                fn direct() {
                    let rows = [Row(3), Row(9)];
                    let ir: Vec<IrRow> = rows.iter().map(IrRow::from_row).collect();
                    let left = analyze(&rows, Mode::All);
                    let right = analyze_ir(&ir, Mode::All);
                    assert_eq!(left.count, right.count); // same implementation
                    assert_eq!(left.names, right.names);
                    assert_eq!(right.names, ["a", "b"]);
                    assert_eq!(left.names, right.count);
                    assert_eq!(left.count, analyze_ir(&ir, Mode::First).count);
                }

                #[test]
                fn paired() {
                    let (left, right) = both(&[Row(4), Row(8)], Mode::All);
                    assert_eq!(left.count, right.count); // same through helper
                    assert_eq!(left.names, right.names);
                    assert_eq!(left.names, ["c", "d"]);
                }
            }
        """, self.rule, ("// same implementation", "// same through helper"), "codec.rs")

    def test_python_empty_direct_parity(self):
        # WHY: empty fixtures leave observable work unexercised even across unrelated APIs.
        # catches: requiring a field access, or tracing emptiness through a mutation.
        self.check_lint("""
            import left
            import right

            def test_parity():
                rows = []
                assert left.analyze(rows) == right.analyze(rows)  # empty list
                assert left.analyze("") == right.analyze("")  # empty text
                assert left.analyze([]) == right.analyze("")
                assert left.analyze([8]) == right.analyze([8])
                rows.append(8)
                assert left.analyze(rows) == right.analyze(rows)
        """, self.rule, ("# empty list", "# empty text"))

    def test_rust_empty_direct_parity(self):
        # WHY: direct equality on an empty fixture has the same gap as field parity.
        # catches: requiring a field, or silently treating different flags or a mutation as equal setup.
        self.check_lint("""
            #[test]
            fn parity() {
                assert_eq!(left::analyze(&[], true), right::analyze(&[], true)); // empty slice
                assert_eq!(left::analyze(vec![]), right::analyze(vec![])); // empty vector
                assert_eq!(left::analyze(""), right::analyze("")); // empty text
                assert_eq!(left::analyze(""), right::analyze(&[]));
                assert_eq!(left::analyze(&[], true), right::analyze(&[], false));
                assert_eq!(left::analyze(&[8]), right::analyze(&[8]));
                let left_result = left::analyze(&[]);
                let right_result = right::analyze(&[]);
                assert_eq!(left_result.len(), right_result.len());
                assert!(right_result.is_empty());

                let mut rows = vec![];
                rows.push(8);
                assert_eq!(left::analyze(&rows), right::analyze(&rows));
            }
        """, self.rule, ("// empty slice", "// empty vector", "// empty text"), "codec.rs")

    def test_python_coupled_codec(self):
        # WHY: a decoder can undo a writer's wire-format error.
        # catches: overlooking the shared fixture through a binding, or ignoring pinned wire bytes.
        self.write("codec.py", """
            def compress(value):
                return transform(value)

            def decompress(wire):
                return inverse(wire)
        """)
        self.check_lint("""
            from codec import compress, decompress

            def test_unpinned():
                source = b"abc"
                wire = compress(source)
                assert decompress(wire) == source  # coupled writer and reader

            def test_pinned():
                source = b"abc"
                wire = compress(source)
                assert decompress(wire) == source
                assert wire == b"123"

            def test_independent_data():
                assert decompress(compress(b"abc")) == b"xyz"
        """, self.rule, ("# coupled writer and reader",))

    def test_rust_coupled_codec(self):
        # WHY: re-encoding data from the matching decoder can hide a shared wire-format error.
        # catches: overlooking the nested codec or treating a pinned decoded value as self-derived.
        self.check_lint("""
            fn encode(value: &[u8]) -> Vec<u8> { value.iter().map(|v| v + 1).collect() }
            fn decode(value: &[u8]) -> Vec<u8> { value.iter().map(|v| v - 1).collect() }

            #[test]
            fn unpinned() {
                let wire = [4, 7];
                assert_eq!(encode(&decode(&wire)), wire); // coupled writer and reader
            }

            #[test]
            fn pinned() {
                let wire = [4, 7];
                let decoded = decode(&wire);
                assert_eq!(decoded, [3, 6]);
                assert_eq!(encode(&decoded), wire);
            }
        """, self.rule, ("// coupled writer and reader",), "codec.rs")

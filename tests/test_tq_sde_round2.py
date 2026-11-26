"""CLI regressions for literal pins and input-sensitive oracle checks."""

from test_tq import TqCase


class SelfDerivedExpectedRound2(TqCase):
    rule = "self-derived-expected"

    def test_python_constant_pins_and_boundary_inputs(self):
        # WHY: pinning a value and checking a boundary relative to a value are independent checks.
        # catches: warning on either check, or treating arithmetic unrelated to the input as a test.
        self.write("codec.py", """
            LIMIT = 7
            FLOOR = 3
            SCALE = 2
            OTHER = 9

            def cap(items):
                return items[:LIMIT]

            def floor(size):
                return max(size, FLOOR)

            def scale(size):
                return max(size, SCALE)

            def other():
                return OTHER
        """)
        self.check_lint("""
            import unittest
            import codec
            from codec import LIMIT as pinned, FLOOR

            class Tests(unittest.TestCase):
                def test_mapping(self):
                    self.assertEqual(len(codec.cap(items)), codec.LIMIT)
                    padding = FLOOR - 1
                    assert codec.floor(padding) == codec.FLOOR
                    values = [number for number in range(codec.SCALE * 2)]
                    assert codec.scale(len(values)) == codec.SCALE
                    unused = codec.OTHER + 20
                    assert codec.other() == codec.OTHER  # unpinned

                def test_pins(self):
                    self.assertEqual(pinned, 7)
                    OTHER = 9
                    assert OTHER == 9
                    assert codec.LIMIT == 7
            """, self.rule, ("# unpinned",))

    def test_rust_constant_pins_and_boundary_inputs(self):
        # WHY: another test can pin a production constant even under an import alias.
        # catches: missing signed or suffixed pins, or exempting setup arithmetic unrelated to a call.
        self.check_lint("""
            const LIMIT: usize = 7;
            const FLOOR: i32 = -3;
            const SCALE: i32 = 2;
            const OTHER: i32 = 9;

            fn cap(items: usize) -> usize { items.min(LIMIT) }
            fn floor(size: i32) -> i32 { size.max(FLOOR) }
            fn scale(size: i32) -> i32 { size.max(SCALE) }
            fn other() -> i32 { OTHER }

            #[cfg(test)]
            mod tests {
                use super::{cap, floor, scale, other, LIMIT as PINNED, FLOOR, SCALE, OTHER};
                #[test]
                fn test_mapping() {
                    assert_eq!(cap(40), super::LIMIT);
                    let padding = FLOOR - 1;
                    assert_eq!(floor(padding), FLOOR);
                    let fixture = vec![0; SCALE * 2];
                    assert_eq!(scale(fixture.len() as i32), SCALE);
                    let unused = OTHER + 20;
                    assert_eq!(other(), OTHER); // unpinned
                }

                #[test]
                fn test_pins() {
                    assert_eq!(7usize, PINNED);
                    let OTHER = 9;
                    assert_eq!(OTHER, 9);
                }
            }
            """, self.rule, ("// unpinned",), "codec.rs")

    def test_python_composition_and_path_pins(self):
        # WHY: an independent helper check anchors wiring, while each path test needs its directory.
        # catches: accepting a sibling path check or missing a literal path in the current test.
        self.write("codec.py", """
            def header():
                return "head"

            def render():
                return "[" + header() + "]"

            def artifact_path(root):
                return root / "artifacts" / "doc.txt"

            def write_artifact(root):
                return artifact_path(root)
        """)
        self.check_lint("""
            import codec

            def test_header():
                assert codec.header() == "head"

            def test_render():
                assert codec.render() == "[" + codec.header() + "]"

            def test_path():
                assert codec.write_artifact(root) == codec.artifact_path(root)  # shared path

            def test_pinned_path():
                path = codec.write_artifact(root)
                assert path == codec.artifact_path(root)
                assert path.parent.name == "artifacts"

            def test_wrong_pin():
                path = codec.write_artifact(root)
                assert path == codec.artifact_path(root)  # wrong segment
                assert "doc.txt" == path.name
            """, self.rule, ("# shared path", "# wrong segment"))

    def test_rust_composition_and_path_pins(self):
        # WHY: literal output pins suppress wiring hits across tests, except for shared paths.
        # catches: passing the path helper off as an independent directory check.
        self.check_lint("""
            use std::path::{Path, PathBuf};

            fn header() -> String { "head".to_string() }
            fn render() -> String { header() + "!" }
            fn artifact_path(root: &Path) -> PathBuf { root.join("artifacts").join("doc.txt") }
            fn write_artifact(root: &Path) -> PathBuf { artifact_path(root) }

            #[test]
            fn test_header() {
                assert_eq!("head", header());
            }

            #[test]
            fn test_render() {
                assert_eq!(render(), header() + "!");
            }

            #[test]
            fn test_path() {
                assert_eq!(write_artifact(root), artifact_path(root)); // shared path
            }

            #[test]
            fn test_pinned_path() {
                let path = write_artifact(root);
                assert_eq!(path, artifact_path(root));
                assert!(path.starts_with(root.join("artifacts")));
            }

            #[test]
            fn test_wrong_pin() {
                let path = write_artifact(root);
                assert_eq!(path, artifact_path(root)); // wrong segment
                assert!(path.ends_with("doc.txt"));
            }
            """, self.rule, ("// shared path", "// wrong segment"), "codec.rs")

    def test_python_field_parity_without_instances(self):
        # WHY: parity of imported frameworks needs a fixture that can contain imports.
        # catches: accepting import-free or empty fixtures, or mistaking a live fixture for empty.
        self.check_lint("""
            import parsed, ir

            def test_import_free():
                source = "def f(): pass"
                left = parsed.analyze(source)
                right = ir.analyze(source)
                assert left.frameworks_detected == right.frameworks_detected  # no imports
                assert left.functions == right.functions

            def test_with_import():
                source = "from flask import Flask"
                assert parsed.analyze(source).frameworks_detected == ir.analyze(source).frameworks_detected

            def test_empty():
                files = []
                assert len(parsed.analyze(files).edges) == len(ir.analyze(files).edges)  # no files

            def test_mutated():
                files = []
                files.append("from flask import Flask")
                assert parsed.analyze(files).imports == ir.analyze(files).imports
            """, self.rule, ("# no imports", "# no files"))

    def test_rust_field_parity_without_instances(self):
        # WHY: identifier-only substitutions cannot add an import to the source template.
        # catches: treating unknown substitutions as identifiers or overlooking an empty fixture.
        self.check_lint(r"""
            fn identifier() -> impl Strategy<Value = String> {
                "[a-z][a-zA-Z]{0,8}".prop_map(|s| s)
            }
            proptest! {
                #[test]
                fn fixture(name in identifier(), unknown in any::<String>()) {
                    let source = format!("function {}() {{}}\n", name);
                    let files = vec![ast::parse_file("app.ts", &source).unwrap()];
                    let ir_files = files.iter().map(IrFile::from_parsed_file).collect();
                    let left = parsed::analyze(&files);
                    let right = ir::analyze(&ir_files);
                    prop_assert_eq!(left.frameworks_detected, right.frameworks_detected); // no imports
                    prop_assert_eq!(left.functions, right.functions);

                    let live = format!("import express from 'express'; function {}() {{}}", name);
                    prop_assert_eq!(parsed::analyze(live).imports, ir::analyze(live).imports);
                    let arbitrary = format!("function {}() {{}}", unknown);
                    prop_assert_eq!(parsed::analyze(arbitrary).imports, ir::analyze(arbitrary).imports);
                }
            }

            #[test]
            fn empty() {
                let files = &[];
                assert_eq!(parsed::analyze(files).edges.len(), ir::analyze(files).edges.len()); // no files
                assert_eq!(parsed::analyze(files).edges.len(), ir::analyze(files).warnings.len());
            }

            fn analyze_both(files: &[&str]) -> (Flow, Flow) {
                let left = parsed::analyze(files);
                let right = ir::analyze(files);
                (left, right)
            }

            #[test]
            fn empty_through_helper() {
                let (left, right) = analyze_both(&[]);
                assert_eq!(left.frameworks_detected, right.frameworks_detected); // empty helper
            }

            #[test]
            fn live_through_helper() {
                let (left, right) = analyze_both(&["import express from 'express'"]);
                assert_eq!(left.frameworks_detected, right.frameworks_detected);
            }
            """, self.rule, ("// no imports", "// no files", "// empty helper"), "codec.rs")

    def test_python_literal_identity(self):
        # WHY: a trim on a trimmed fixture cannot distinguish any deterministic implementation.
        # catches: assuming a transform is an identity on every input or following a shadowed helper.
        self.write("codec.py", """
            def normalize(value):
                return value.strip()
        """)
        self.check_lint("""
            import codec
            from codec import normalize as clean

            def test_normalized():
                assert clean("ready") == codec.normalize(clean("ready"))  # nested identity
                assert clean(" ready ") == clean(clean(" ready "))
                assert clean("ready") == clean("ready".strip())  # trimmed literal
                assert clean(" ready ") == clean(" ready ".strip())
                assert clean("ready") == clean("ready".upper())
                assert clean("ready") == clean("ready".replace("a", "b"))
            """, self.rule, ("# nested identity", "# trimmed literal"))

    def test_rust_literal_identity(self):
        # WHY: the returned trim result is a fixed point only for fixtures without edge whitespace.
        # catches: reporting a real second pass or overlooking an explicit identity transform.
        self.check_lint("""
            fn normalize(value: &str) -> &str { value.trim() }

            #[test]
            fn normalized() {
                assert_eq!(normalize("ready"), normalize(normalize("ready"))); // nested identity
                assert_eq!(normalize(" ready "), normalize(normalize(" ready ")));
                assert_eq!(normalize("ready"), normalize("ready".trim())); // trimmed literal
                assert_eq!(normalize(" ready "), normalize(" ready ".trim()));
            }
            """, self.rule, ("// nested identity", "// trimmed literal"), "codec.rs")

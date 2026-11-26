"""CLI regressions for the self-derived-expected advisory."""

from test_tq import TqCase


class SelfDerivedExpectedRound1(TqCase):
    rule = "self-derived-expected"

    def test_python_aliases_and_test_local_extractors(self):
        # WHY: two aliases of one callee can express a property.
        # catches: comparing alias spelling or using a test helper as production provenance.
        self.check_lint("""
            import codec
            from codec import encode as encode_again
            from tests import helpers

            def bits(value):
                return value.hex()

            def test_properties():
                baseline = codec.encode(v1)
                assert baseline == encode_again(v2)
                assert baseline == codec.encode(v1)
                extract = lambda value: value[0]
                assert extract(codec.encode(v1)) == extract(reference)
                assert bits(codec.encode(v1)) == bits(reference)
                assert helpers.extract(v1) == helpers.reference(v2)
                assert baseline == codec.oracle(v1)  # distinct production calls
            """, self.rule, ("# distinct production calls",))

    def test_rust_aliases_and_test_local_extractors(self):
        # WHY: imports from a parent module can name either production code or a test helper.
        # catches: treating differently spelled calls or helpers from a cfg(test) module as production.
        self.check_lint("""
            fn encode(x: u8) -> u8 { x + 1 }
            fn oracle(x: u8) -> u8 { x + 2 }
            #[cfg(test)]
            mod support {
                pub fn extract(x: u8) -> u8 { x }
                pub fn reference(x: u8) -> u8 { x }
            }
            #[cfg(test)]
            mod tests {
                use super::*;
                use super::encode as encode_again;

                #[test]
                fn properties() {
                    let baseline = encode(1);
                    assert_eq!(baseline, encode_again(2));
                    assert_eq!(baseline, super::encode(1));
                    let extract = |value| value;
                    assert_eq!(extract(encode(1)), extract(0));
                    assert_eq!(super::support::extract(1), super::support::reference(2));
                    assert_eq!(baseline, oracle(1)); // distinct production calls
                }
            }
            """, self.rule, ("// distinct production calls",), "codec.rs")

    def test_python_transparent_accessors(self):
        # WHY: serializing both sides preserves their underlying provenance.
        # catches: stopping at the wrapper or treating independent file reads as coupled.
        self.check_lint("""
            import codec
            import json
            import reference
            from json import dumps as dump

            def test_wrappers():
                assert sorted(list(codec.encode(v1))) == sorted(list(codec.encode(v2)))
                assert first.read_bytes() == second.read_bytes()
                assert json.dumps(codec.encode(v1)) == dump(reference.encode(v1))
                assert repr(codec.encode(v1)).tobytes() == str(codec.oracle(v1))  # wrapped
                assert codec.encode(v1).to_json() == bytes(codec.oracle(v1))  # serialized
            """, self.rule, ("# wrapped", "# serialized"))

    def test_rust_transparent_accessors(self):
        # WHY: conversions and iteration can hide the calls whose results are compared.
        # catches: stopping at a generic collect or coupling std file reads.
        self.check_lint("""
            use std::fs::read_to_string as read;

            #[test]
            fn wrappers() {
                assert_eq!(read(first), std::fs::read_to_string(second));
                assert_eq!(codec::encode(v1).clone(), codec::encode(v2).clone());
                assert_eq!(codec::encode(v1).clone().to_string(),
                           codec::oracle(v1).to_string());
                assert_eq!(codec::encode(v1).iter().collect::<Vec<_>>(), codec::oracle(v1)); // collected
                for rebinding in inputs {
                    let rebinding = rebinding.clone();
                    assert_eq!(rebinding, codec::oracle(v1));
                }
            }
            """, self.rule, ("codec::encode(v1).clone().to_string()", "// collected"), "checks.rs")

    def test_python_composed_oracle(self):
        # WHY: both chunks of an expected string can come from production.
        # catches: looking only at the outer expression or counting calls inside unrelated setup.
        self.check_lint("""
            import web_page
            import reference

            def test_composition():
                tail = web_page.body(item)
                assert web_page.render(item) == web_page.header(item) + tail  # concatenated
                assert web_page.render(item) == f"<p>{web_page.body(item)}</p>"  # interpolated
                assert web_page.render(item) == reference.render(web_page.body(item))
                assert web_page.render(item) == reference.header(item) + tail
                assert web_page.render(item) == web_page.render(other) + b""
                assert web_page.render(tail) == tail
                rendered = web_page.render(tail)
                assert rendered.body == tail.body
            """, self.rule, ("# concatenated", "# interpolated"))

    def test_rust_composed_oracle(self):
        # WHY: a binary expression can reuse two functions from the same impl.
        # catches: skipping the impl path or following a production call through a different oracle.
        self.check_lint("""
            struct Codec;
            impl Codec {
                fn render(x: u8) -> u8 { Self::header(x) + Self::body(x) }
                fn header(x: u8) -> u8 { x / 2 }
                fn body(x: u8) -> u8 { x - x / 2 }
            }
            #[cfg(test)]
            mod tests {
                use super::Codec as Encoder;

                #[test]
                fn composition() {
                    let tail = Encoder::body(4);
                    assert_eq!(Encoder::render(4), Encoder::header(4) + tail); // concatenated
                    assert_eq!(Encoder::render(4), reference::render(Encoder::body(4)));
                    assert_eq!(Encoder::render(4), reference::header(4) + tail);
                    assert_eq!(Encoder::render(4), Encoder::render(5) + 0);
                    assert_eq!(Encoder::header(tail), tail);
                    let rendered = Encoder::render(tail);
                    assert_eq!(rendered.to_string(), tail.to_string());
                }
            }
            """, self.rule, ("// concatenated",), "codec.rs")

    def test_python_constants_used_by_production(self):
        # WHY: the source must show that the function reads the same constant.
        # catches: guessing from uppercase names or from neighboring modules.
        self.write("codec.py", """
            DEFAULT_LIMIT = 7
            TABLE = (3, 6, 12)
            OTHER = 7

            def limit():
                return DEFAULT_LIMIT

            def value(index):
                return TABLE[index]

            def shadowed():
                DEFAULT_LIMIT = 9
                return DEFAULT_LIMIT
            """)
        self.check_lint("""
            import codec
            import reference
            from codec import DEFAULT_LIMIT as default

            def test_constants():
                assert codec.limit() == default  # shared limit
                assert codec.value(2) == codec.TABLE[2]  # shared table
                assert codec.limit() == codec.OTHER
                assert codec.limit() == reference.DEFAULT_LIMIT
                assert codec.shadowed() == default
                default_limit = 7
                assert codec.limit() == default_limit
            """, self.rule, ("# shared limit", "# shared table"))

    def test_rust_constants_used_by_production(self):
        # WHY: resolving a constant imported from the parent must preserve its identity.
        # catches: using an identically named constant or a name shadowed by a local.
        self.check_lint("""
            const DEFAULT_LIMIT: u8 = 7;
            const TABLE: [u8; 3] = [3, 6, 12];
            const OTHER: u8 = 7;

            fn limit() -> u8 { DEFAULT_LIMIT }
            fn value(index: usize) -> u8 { TABLE[index] }
            fn shadowed() -> u8 {
                let DEFAULT_LIMIT = 9;
                DEFAULT_LIMIT
            }

            #[cfg(test)]
            mod tests {
                use super::{limit, shadowed, value, DEFAULT_LIMIT as DEFAULT, OTHER, TABLE};

                #[test]
                fn constants() {
                    assert_eq!(limit(), DEFAULT); // shared limit
                    assert_eq!(value(2), TABLE[2]); // shared table
                    assert_eq!(limit(), OTHER);
                    assert_eq!(limit(), reference::DEFAULT_LIMIT);
                    assert_eq!(shadowed(), DEFAULT);
                    let DEFAULT = 7;
                    assert_eq!(limit(), DEFAULT);
                }
            }
            """, self.rule, ("// shared limit", "// shared table"), "codec.rs")

    def test_rust_helpers_in_a_dedicated_test_file(self):
        # WHY: a dedicated test file can import its own fixture readers.
        # catches: attributing expected fixture data to production because both helpers share a module.
        self.check_lint("""
            mod support;
            use support::{fixture, run};

            fn local_value() -> u8 { 4 }
            fn local_reference() -> u8 { 4 }

            #[test]
            fn fixtures() {
                assert_eq!(run("input"), fixture("expected"));
                assert_eq!(local_value(), local_reference());
                assert_eq!(codec::encode(input), codec::oracle(input)); // production calls
            }
            """, self.rule, ("// production calls",), "codec_tests.rs")

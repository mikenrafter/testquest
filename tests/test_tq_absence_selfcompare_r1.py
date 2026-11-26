"""Regression fixtures for recorder controls and comparisons through aliases."""
from test_tq import TqCase


class GatingRules(TqCase):
    def check_rows(self, filename, source, rule):
        path = self.write(filename, source)
        expected = [i for i, line in enumerate(path.read_text().splitlines(), 1)
                    if line.endswith("flag here")]
        actual = [line for _, line, code, _ in self.findings("lint", path.name) if code == rule]
        self.assertEqual(actual, expected)

    def test_python_recorder_controls(self):
        # WHY: only a positive check on the same callback slot proves it can record.
        # catches: keeping controlled absences or accepting unrelated positive checks
        self.check_rows("test_recorders.py", """
            def test_writes_on_acceptance():
                rejected = []
                unrelated = []
                subscribe(rejected.append, accepted=False)
                report(unrelated.append)
                assert unrelated == ["message"]
                assert not rejected  # flag here

            def test_producer_with_control():
                rejected = []
                accepted = []
                subscribe(rejected.append, accepted=False)
                subscribe(accepted.append, accepted=True)
                assert accepted == ["message"]
                assert not rejected

            def test_direct_write_is_not_producer_evidence():
                rejected = []
                accepted = []
                accepted.append("message")
                subscribe(accepted.append, accepted=True)
                subscribe(rejected.append, accepted=False)
                assert accepted == ["message"]
                assert not rejected  # flag here

            def test_literal_cases():
                failures = []
                for raw, want in [("one", 1), ("two", 2)]:
                    if parse(raw) != want:
                        failures.append(raw)
                assert failures == []

            def test_detector():
                failures = []
                for bug in detect_all():
                    failures.append(bug)
                assert failures == []  # flag here
            """, "absence-no-control")

    def test_rust_recorder_controls(self):
        # WHY: a fresh vector can serve as a control when the same producer fills it.
        # catches: ignoring nonempty iterator checks or confusing different producer methods
        self.check_rows("checks.rs", """
            #[test]
            fn controlled() {
                let mut accepted = Vec::new();
                let mut rejected = Vec::new();
                server.send(&mut accepted, true);
                assert!(accepted.iter().any(|msg| msg.valid()));
                server.send(&mut rejected, false);
                assert!(rejected.is_empty());
            }

            #[test]
            fn unrelated() {
                let mut accepted = Vec::new();
                let mut rejected = Vec::new();
                server.send(&mut accepted, true);
                assert_eq!(accepted.len(), 2);
                another.send(&mut rejected, false);
                assert!(rejected.is_empty()); // flag here
            }

            #[test]
            fn literal_cases() {
                let mut failures = Vec::new();
                for (raw, want) in [("one", 1), ("two", 2)] {
                    if parse(raw) != want {
                        failures.push(raw);
                    }
                }
                assert!(failures.is_empty());
            }

            #[test]
            fn detector() {
                let mut failures = Vec::new();
                for bug in detect_all() {
                    failures.push(bug);
                }
                assert!(failures.is_empty()); // flag here
            }
            """, "absence-no-control")

    def test_python_unobserved_callbacks(self):
        # WHY: a test can pass without checking a callback flag or count.
        # catches: restricting write-only recorders to lists or ignoring a read
        self.check_rows("test_callbacks.py", """
            def test_unread_flag():
                called = False
                def forward():
                    nonlocal called
                    called = True  # flag here
                assert reject(forward)

            def test_unread_count():
                count = 0
                def forward():
                    nonlocal count
                    count += 1  # flag here
                assert reject(forward)

            def test_read_callback_count():
                count = 0
                def forward():
                    nonlocal count
                    count += 1
                accept(forward)
                assert count == 1

            def test_unread_list():
                called = []
                assert reject(lambda: called.append(True))  # flag here

            def test_local_flag():
                called = False
                def forward():
                    called = True
                    return called
                assert accept(forward)
            """, "absence-no-control")

    def test_rust_unobserved_callbacks(self):
        # WHY: a test can pass without checking writes through a closure or Cell.
        # catches: treating an assignment as a read or treating Cell::get as a write
        self.check_rows("checks.rs", """
            #[test]
            fn unread_flag() {
                let mut called = false;
                let result = reject(|| { called = true; }); // flag here
                assert!(result);
            }

            #[test]
            fn unread_cell() {
                let called = Cell::new(0);
                let result = reject(|| called.set(1)); // flag here
                assert!(result);
            }

            #[test]
            fn unread_cell_increment() {
                let count = std::cell::Cell::new(0);
                let result = reject(|| count.set(count.get() + 1)); // flag here
                assert!(result);
            }

            #[test]
            fn read_cell() {
                let called = Cell::new(false);
                accept(|| called.set(true));
                assert!(called.get());
            }

            #[test]
            fn unread_count() {
                let mut count = 0;
                let result = reject(|| { count += 1; }); // flag here
                assert!(result);
            }

            #[test]
            fn unread_vec() {
                let mut calls = vec![];
                assert!(reject(|| calls.push(true))); // flag here
            }

            #[test]
            fn ordinary_counter() {
                let mut count = 0;
                count += 1;
                assert!(reject());
            }
            """, "absence-no-control")

    def test_python_self_compare(self):
        # WHY: aliases add no independent expected value, while deliberate repeat calls can.
        # catches: resolving distinct calls as aliases or ignoring rebinding and field writes
        self.check_rows("test_comparisons.py", """
            def test_alias_and_fields(self):
                actual = read()
                expected = actual
                alias = expected
                self.assertEqual(actual, alias)  # flag here
                self.assertEqual(actual, actual)  # flag here
                assert actual.name == actual.name  # flag here
                assert actual.name != actual.label

            def test_changes(self):
                actual = read()
                expected = actual
                actual = read()
                self.assertEqual(actual, expected)

            def test_field_changes():
                actual = read()
                expected = actual.name
                actual.name = "other"
                assert actual.name != expected

            def test_repeats_names():
                assert render("a") == render("a")

            def test_companion(self):
                assert render("a").name == render("a").name
                self.assertNotEqual(render("b").name, render("a").name)
                assert decode() == decode()
                assert decode() == ["x", "y"]
                assert parse("a") == parse("a")  # flag here
                assert parse("b") != parse("c")

            def test_comment():
                assert render("a") == render("a")  # Stable output.
                assert value == value  # Consistent result, flag here
            """, "self-compare")

    def test_rust_self_compare(self):
        # WHY: same-binding comparisons survive spelling changes but clones can diverge.
        # catches: flagging checked repeatability or equating a modified clone with its source
        self.check_rows("checks.rs", """
            #[test]
            fn aliases_and_fields() {
                let actual = read();
                let expected = actual.clone();
                let alias = expected;
                assert_eq!(alias, actual); // flag here
                assert!(actual.name == actual.name); // flag here
                assert_ne!(actual.name, actual.label);
            }

            #[test]
            fn changes() {
                let mut actual = read();
                let expected = actual.clone();
                actual.name = "other";
                assert_ne!(actual, expected);
                let mut names = load();
                let previous = names.clone();
                names.push("more");
                assert_ne!(previous, names);
                let mut frames = load();
                let snapshot = frames.clone();
                frames[0] = 10;
                assert_ne!(snapshot, frames);
            }

            #[test]
            fn stable_output() {
                assert_eq!(render(), render());
            }

            #[test]
            fn companion() {
                assert_eq!(render("a").name, render("a").name);
                assert_ne!(render("b").name, render("a").name);
                assert_eq!(decode(), decode());
                assert_eq!(decode(), ["x", "y"]);
                assert_eq!(parse("a"), parse("a")); // flag here
                assert_ne!(parse("b"), parse("c"));
            }

            #[test]
            fn comment() {
                assert_eq!(render(), render()); // Reproducible output.
                assert_eq!(value, value); // Identical value, flag here
            }
            """, "self-compare")

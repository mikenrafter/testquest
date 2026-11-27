def test_multiple_groups():
    assert "alpha" in report.text()
    assert "one" in table
    assert "beta" in report.text()
    assert 'gamma\n' in report.text()
    assert "two" in table
    assert "three" in table
    assert "four" in table


def test_clean(self):
    self.assertIn(member="alpha", container=out)
    self.assertIn("beta", out)
    self.assertIn("gamma", out)
    self.assertEqual(expected, out)
    self.assertIn("one", other)
    self.assertIn("two", other)
    for needle in fragments:
        self.assertIn(needle, other)
    assert "three" in misc


def test_wrong_subject(self):
    self.assertEqual(expected, out)
    self.assertIn(member="alpha", container=other)
    self.assertIn("beta", other)
    self.assertIn("gamma", other)


def test_mixed_golden(self):
    self.assertIn("alpha", render())
    self.assertIn("beta", render())
    self.assertIn("gamma", render())
    golden.check(self, "rendered", render(), self.paths())


def test_markdown_project_rows(self):
    rows = render_rows("shrunk")
    self.assertEqual(sum(row.startswith("| ") for row in rows), 4)
    text = "\n".join(rows)
    self.assertIn("changed from weighted to shrunk", text)
    self.assertIn("This split does not choose the method.", text)
    text = "\n".join(render_rows("weighted"))
    self.assertIn("still favors weighted", text)

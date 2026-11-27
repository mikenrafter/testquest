def test_multiple_groups():
    assert "alpha" in report.text()
    assert "one" in log
    assert "beta" not in report.text()
    assert 'gamma\n' in report.text()
    assert "two" in log
    assert "three" in log
    assert "four" in log


def test_clean(self):
    self.assertIn(member="alpha", container=out)
    self.assertNotIn("beta", out)
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
    self.assertNotIn("beta", other)
    self.assertIn("gamma", other)


def test_mixed_golden(self):
    self.assertIn("alpha", render())
    self.assertNotIn("beta", render())
    self.assertIn("gamma", render())
    golden.check(self, "rendered", render(), self.paths())

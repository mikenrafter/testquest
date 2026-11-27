class SkillWatchTests:
    def test_installed_source_change_says_follow_it_now(self):
        # WHY: a source change that the install already copied is live; the old wording
        # ("not yet installed", "only if the owner asked") let an agent discount it.
        # catches: labelling every mstack-source change as uninstalled
        (self.source / "demo").mkdir(parents=True)
        (self.source / "demo" / "SKILL.md").write_text("v1\n")
        self.run_hook()
        (self.source / "demo" / "SKILL.md").write_text("v2\n")
        (self.skill / "SKILL.md").write_text("v2\n")
        alert = self.run_hook()
        self.assertIn("mstack source, installed", alert)
        self.assertIn("follow them now", alert)
        self.assertNotIn("not yet installed", alert)
        self.assertNotIn("only if the owner asked", alert)

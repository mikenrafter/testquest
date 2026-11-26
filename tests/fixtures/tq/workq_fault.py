def test_workq_suite():
    # catches: remove the exclusive queue lock [patch: workq-no-lock.patch]
    run_workq_suite()

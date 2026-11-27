#[test]
fn fragments() {
    assert!(response.text().contains("alpha"));
    assert!(!response.text().contains(r#"beta"#));
    assert!(response.text().contains("gamma"), "missing gamma");
}

#[test]
fn whole() {
    assert!(out.contains("alpha"));
    assert!(out.contains("beta"));
    assert!(out.contains("gamma"));
    assert_eq!(expected, &out);
}

#[test]
fn snapshot() {
    assert!(out.contains("alpha"));
    assert!(out.contains("beta"));
    assert!(out.contains("gamma"));
    insta::assert_snapshot!("output", out);
}

#[test]
fn inline_snapshot() {
    assert!(out.contains("alpha"));
    assert!(out.contains("beta"));
    assert!(out.contains("gamma"));
    insta::assert_snapshot!(out, @"alpha beta gamma");
}

#[test]
fn reads_each_binding_once_for_grouping() {
    let err = resolve("deploy prod").unwrap_err().to_string();
    assert!(err.contains("not a valid name"));
    assert!(err.contains("deploy prod"));
    let err = resolve("-version").unwrap_err().to_string();
    assert!(err.contains("not a valid name"));
    let diff = simple_unified_diff("", "new line 1\nnew line 2");
    assert!(diff.contains("+new line 1"));
    assert!(diff.contains("+new line 2"));
    assert!(!diff.contains("-"));
}

#[test]
fn migration_checks_fixture_header() {
    fs::write(dir.join("residues.csv"), "force,auth,db\n").unwrap();
    migrate(&dir);
    let residues = fs::read_to_string(dir.join("residues.csv")).unwrap();
    assert!(residues.contains("auth"));
}

#[test]
fn whole_comparison_covers_only_its_render() {
    let text = render("before");
    assert_eq!(text, expected);
    let text = render("after");
    assert!(text.contains("Header"));
    assert!(text.contains("Rows"));
    assert!(text.contains("Footer"));
}

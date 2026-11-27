#[test]
fn fragments() {
    assert!(response.text().contains("alpha"));
    assert!(!response.text().contains(r#"beta"#));
    assert!(response.text().contains("gamma"), "missing gamma");
}

#[test]
fn whole() {
    assert!(out.contains("alpha"));
    assert!(!out.contains("beta"));
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

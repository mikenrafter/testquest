#[test]
fn test_strings_without_assertions() {
    let normal = "end quote: \" assert!(true); }";
    let byte_string = b"assert!(true)";
    let raw = r##"quote " and delimiter "# and #[test] fn fake() { assert!(true); }"##;
    let byte_raw = br#"assert!(true)"#;
    let close_brace = '}';
    let apostrophe = '\'';
    let byte_brace = b')';
    // assert!(true);
    /* outer /* inner */ } assert!(true); still commented out #[test] */
    consume((normal, byte_string, raw, byte_raw, close_brace, apostrophe, byte_brace));
}

#[tokio::test]
async fn nested_arguments<'a>() {
    let joined = join::<'a>(r#"left, )"#, "right");
    assert_eq!(
        assemble!((3, 5), [7, 11], { joined }),
        assemble!((3, 5), [7, 11], { joined }),
        "literal: assert!(true)"
    );
}

fn ordinary() {
    assert!(true);
}

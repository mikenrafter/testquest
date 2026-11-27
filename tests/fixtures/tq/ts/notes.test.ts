// tq: allow no-assert this test checks successful execution
test("smoke", () => { launch(); });
test("branch with witness", () => {
  // reaches: reaches the branch
  if (ready()) expect(read()).toBe(5);
});
test("branch alone", () => {
  if (ready()) expect(read()).toBe(5);
});
test("reaches the branch", () => {
  /* catches: wrong value */
  expect(read()).toBe(5);
});
test("block suppression", () => {
  /* tq: allow const-truth fixture deliberately checks matcher plumbing */
  assert(1);
});
test("multiline note", () => {
  /**
   * # catches: throws the wrong error
   */
  expect(read).toThrow();
});
test("outer", () => {
  expect(read()).toBe(5);
  test("inner", () => {
    /* catches: nested fault */
    expect(read()).toBe(8);
  });
});

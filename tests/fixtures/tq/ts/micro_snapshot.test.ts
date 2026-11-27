test("fragments", () => {
  expect(response.text).toContain("alpha");
  expect(response.text).not.toContain("beta");
  expect(response.text).toMatch(`gamma`);
  expect(response.text).toMatch(/regex/);
  expect(response.text).toMatch(`${dynamic}`);
});

test("whole", () => {
  expect(out).toContain("alpha");
  expect(out).not.toContain("beta");
  expect(out).toContain("gamma");
  expect(out).toBe(expected);
});

test("snapshot", () => {
  expect(out).toContain("alpha");
  expect(out).toContain("beta");
  expect(out).toContain("gamma");
  expect(out).toMatchInlineSnapshot();
});

test("negative equality", () => {
  expect(out).toContain("alpha");
  expect(out).toContain("beta");
  expect(out).toContain("gamma");
  expect(out).not.toEqual(expected);
});

test("nested checks", () => {
  expect(out).toContain("alpha");
  expect(out).toContain("beta");
  const deferred = () => expect(out).toContain("gamma");
});

test("same line one", () => { expect(out).toContain("a"); expect(out).toContain("b"); expect(out).toContain("c"); }); test("same line two", () => { expect(log).toContain("a"); expect(log).toContain("b"); expect(log).toContain("c"); });

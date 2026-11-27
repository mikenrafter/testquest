import { replyLines as importedLines } from "./micro_snapshot_helper.js";

test("fragments", () => {
  expect(response.text).toContain("alpha");
  expect(response.text).not.toContain("beta");
  expect(response.text).toMatch(`gamma`);
  expect(response.text).toMatch(/regex/);
  expect(response.text).toMatch(`${dynamic}`);
});

test("whole", () => {
  expect(out).toContain("alpha");
  expect(out).toContain("beta");
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

function replyLines(text: string): string[] { return text.split("\n"); }

test("list membership, constant strings and repeated reads", () => {
  const lines = replyLines(command.stdout);
  expect(lines).toContain("header");
  expect(lines).toContain("counts");
  expect(lines).toContain("notice");

  const expectedWhen = "active && !editorFocus && !inputFocus";
  expect(expectedWhen).toContain("active"); // tq: allow const-truth fixture deliberately searches its own literal
  expect(expectedWhen).toContain("!editorFocus"); // tq: allow const-truth fixture deliberately searches its own literal
  expect(expectedWhen).toContain("!inputFocus"); // tq: allow const-truth fixture deliberately searches its own literal

  expect(bar().textContent).toContain("Next:");
  expect(bar().textContent).toContain("12:18");
  setSystemTime();
  expect(bar().textContent).toContain("11:18");

  const report = renderReport();
  expect(report).toContain("Summary");
  expect(report).toContain("Not served");
  expect(report).toContain("Tool count");
});

test("conflicting branches observe different diagnostics", () => {
  const projected = readRuntimeVersion();
  if (isNode()) {
    expect(projected.message).toContain("heap limit");
    expect(projected.message).toContain("MiB");
  } else {
    expect(projected.message).toContain("ignores the flag");
  }
});

test("independent reads after assigning the same text variable", () => {
  let report = renderReport("before");
  expect(report).toContain("Header");
  expect(report).toContain("Rows");
  report = renderReport("after");
  expect(report).toContain("Footer");
});

test("typed membership through a helper module", () => {
  const lines = importedLines(command.stdout);
  expect(lines).toContain("header");
  expect(lines).toContain("counts");
  expect(lines).toContain("notice");

  const readme = file(planned.value, "README.md");
  expect(readme).toContain("Not served");
  expect(readme).toContain("Tool count");
  expect(readme).toContain("Neither is emulation");
});

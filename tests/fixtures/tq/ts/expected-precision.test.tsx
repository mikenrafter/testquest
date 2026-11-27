import { render } from "@testing-library/react";
import assert from "node:assert/strict";

function caption(mode: string) {
  const { container } = render(<Preview mode={mode} />);
  return container.textContent ?? "";
}

it("restores the chosen preview", () => {
  const chosen = caption("chosen");
  expect(caption("other")).not.toBe(chosen);
  expect(caption("restored")).toBe(chosen);
});

it("uses strict equality for restoring a preview", () => {
  const chosen = caption("chosen");
  expect(chosen).not.toStrictEqual(caption("other"));
  expect(caption("restored")).toStrictEqual(chosen);
});

it("compares captured renders with Node assertions", () => {
  const chosen = caption("chosen");
  assert.notEqual(caption("other"), chosen);
  assert.equal(caption("restored"), chosen);

  const compact = caption("compact");
  assert.notDeepEqual(compact, caption("large"));
  assert.deepEqual(caption("reopened"), compact);
});

it("uses the rendered quantity as its oracle", () => {
  const total = Number(caption("before"));
  const unrelated = caption("other");
  expect(caption("other size")).not.toEqual(unrelated);
  expect(caption("after")).toEqual(`${total - 1}`);
  expect(caption("reopened")).toBe(total);
});

it("distinguishes the first capture only", () => {
  let captured = caption("chosen");
  expect(caption("other")).not.toEqual(captured);
  expect(caption("restored")).toEqual(captured);
  {
    const captured = caption("different");
    expect(caption("restored")).toEqual(captured);
  }
});

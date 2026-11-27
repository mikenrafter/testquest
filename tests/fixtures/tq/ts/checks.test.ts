import verify from "node:assert/strict";
import { assert as chaiAssert, expect as chaiExpect } from "chai";
import { test as scenario } from "node:test";
function checkOutput() { verify.strictEqual(read(), 9); }
function checkNothing() { read(); }
scenario("context", async (context) => {
  await context.test("subtest", t => { t.assert.strictEqual(read(), 9); });
});
it("ava context", t => t.deepEqual(read(), [9]));
test("assert import", () => verify(read()));
test("chai assert", () => chaiAssert.isOk(read()));
test("chai expect", () => chaiExpect(read()).to.deep.equal([9]));
test("chai should", () => read().should.equal(9));
test("throw", () => { throw new Error("wrong"); });
test("expect plan", () => expect.assertions(1));
test("expect plan minimum", () => expect.hasAssertions());
test("helper", () => checkOutput());
test("empty helper", () => checkNothing());
test("same", () => expect(reading).toBe((reading)));
test("same assert", () => verify.equal(value, value));
test("constant matcher", () => expect(true).toBe(true));
test("constant assert", () => verify(1));
test("opposite", () => expect(value).not.toBe(value));
test("snapshot properties", () => expect(value).toMatchSnapshot(value));
function verifyOutput() { verify.strictEqual(read(), 9); }
test("local verifier", () => verifyOutput());
import fc from "fast-check";
test("property callback", () => fc.assert(fc.property(fc.integer(), n => read(n) > 0)));
test("chai property", () => chaiExpect(read()).to.be.true);
test("deterministic output for one input", () => expect(render(input)).toBe(render(input)));
test("different inputs produce the same output", () => expect(render(input)).toBe(render(input)));

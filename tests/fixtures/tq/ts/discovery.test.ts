import { test as spec, describe as group, expect } from "vitest";
group.only("outer", () => {
  group(`inner`, () => {
    spec.each([[1]])("parameter %s", (x) => { consume(x); });
    spec.concurrent("prints", () => console.log("no check"));
    spec.skip("ignored", () => expect(true).toBe(true));
    spec.todo("later");
    test("wrapper", () => {
      test("nested", () => expect(result()).toMatchInlineSnapshot());
    });
  });
});
import { test as browser } from "@playwright/test";
browser.describe.skip("headless", () => {
  browser("ignored browser", () => consume());
});
browser.describe.parallel("browser group", () => {
  browser("action only", async () => { await doAction(); });
});

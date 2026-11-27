test("detached chain", () => { fetch().then(value => expect(value).toBe(1)); });
test("returned chain", () => fetch().then(value => expect(value).toBe(1)));
test("awaited chain", async () => { await fetch().then(value => expect(value).toBe(1)); });
test("detached matcher", async () => { expect(fetch()).resolves.toBe(1); });
test("returned matcher", () => expect(fetch()).rejects.toThrow());
test("awaited matcher", async () => { await expect(fetch()).resolves.toBe(1); });
test("async map", async () => { await [1].map(async x => expect(await read(x)).toBe(1)); });
test("joined map", async () => { await Promise.all([1].map(async x => expect(await read(x)).toBe(1))); });
test("detached helper", () => { checkLater(); });
test("returned helper", () => checkLater());
async function checkLater() { expect(await fetch()).toBe(1); }
test("stored matcher", async () => { const result = expect(fetch()).resolves.toBe(1); await result; });
test("lost matcher", async () => { checkLater(); await fetch(); });
test("detached closure", () => { (async () => expect(await fetch()).toBe(1))(); });
test("returned closure", () => (async () => expect(await fetch()).toBe(1))());
test("detached executor", () => { new Promise((done) => { expect(out()).toBe(1); done(); }); });
test("returned executor", () => new Promise((done) => { expect(out()).toBe(1); done(); }));

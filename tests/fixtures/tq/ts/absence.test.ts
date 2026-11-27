test("uncontrolled spy", () => {
  expect(other).toHaveBeenCalled();
  expect(spy).not.toHaveBeenCalled();
});
test("same spy", () => {
  expect(spy).toHaveBeenCalledTimes(1);
  spy.mockClear();
  expect(spy).not.toHaveBeenCalled();
});
test("uncontrolled length", () => {
  expect(list).toHaveLength(0);
  expect(other).toHaveLength(1);
});
test("same recorder through calls", () => {
  expect(spy.mock.calls).toHaveLength(1);
  expect(spy).toHaveBeenCalledTimes(0);
});
test("guarded control", () => {
  if (ready()) expect(spy).toHaveBeenCalled();
  expect(spy).not.toHaveBeenCalled();
});
test("recorder alias", () => {
  const alias = spy;
  expect(spy).toHaveBeenCalled();
  expect(alias).not.toHaveBeenCalled();
});
test("shadowed recorder", () => {
  const spy = makeSpy();
  expect(spy).toHaveBeenCalled();
  {
    const spy = makeDifferentSpy();
    expect(spy).not.toHaveBeenCalled();
  }
});

void main() {
  test('fragments', () {
    expect(response.text(), contains('alpha'));
    expect(response.text(), isNot(contains('beta')));
    expect(response.text(), contains(r'gamma'));
    expect(response.text(), contains('$dynamic'));
  });

  test('whole', () {
    expect(out, contains('alpha'));
    expect(out, isNot(contains('beta')));
    expect(out, contains('gamma'));
    expect(out, equals(expected));
  });

  test('golden', () {
    expect(out, contains('alpha'));
    expect(out, contains('beta'));
    expect(out, contains('gamma'));
    expectLater(out, matchesGoldenFile('output.txt'));
  });
}

void main() {
  test('fragments', () {
    expect(response.text(), contains('alpha'));
    expect(response.text(), isNot(contains('beta')));
    expect(response.text(), contains(r'gamma'));
    expect(response.text(), contains('$dynamic'));
  });

  test('whole', () {
    expect(out, contains('alpha'));
    expect(out, contains('beta'));
    expect(out, contains('gamma'));
    expect(out, equals(expected));
  });

  test('golden', () {
    expect(out, contains('alpha'));
    expect(out, contains('beta'));
    expect(out, contains('gamma'));
    expectLater(out, matchesGoldenFile('output.txt'));
  });

  test('checks a list and its mapped elements', () {
    final List<String> dates = loadDates();
    expect(dates, contains('April 3'));
    expect(dates, contains('April 2'));
    expect(dates, contains('April 1'));

    final kinds = violations.map((violation) => violation.kind);
    expect(kinds, contains('one'));
    expect(kinds, contains('two'));
    expect(kinds, contains('three'));

    final report = renderReport();
    expect(report, contains('Date'));
    expect(report, contains('Kinds'));
    expect(report, contains('Summary'));
  });
}

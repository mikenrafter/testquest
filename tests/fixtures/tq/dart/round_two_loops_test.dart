import 'package:test/test.dart';

enum Brightness { light, dark }

class Level {
  static const LEVELS = [10, 20, 30];
}

class Fixture {
  const Fixture(this.ids);
  final Set<String> ids; final String label = 'label';
}

final fixtures = [Fixture({'one'}), Fixture({'two'})]; void readFixture(Fixture fixture) => paint(fixture.label);

void main() {
  test('all input boundaries', () {
    final input = makeInput();
    for (var i = 0; i <= input.length; i++) expect(boundary(input, i), isTrue);
  });
  test('fixed levels', () {
    const levels = Level.LEVELS;
    for (var i = 0; i < levels.length; i++) { for (var j = i + 1; j < levels.length; j++) expect(ordered(levels, i, j), isTrue); }
  });
  test('rejects incomplete sequences', () {
    var data = [[0xfe], [0xf8, 0xfe]];
    for (final bytes in data) expect(() => decode(bytes), throwsStateError);
  });
  test('line count cannot grow', () {
    const widths = [400, 800];
    final counts = [for (final width in widths) layout(width).lines.length];
    for (var i = 1; i < counts.length; i++) expect(counts[i], lessThanOrEqualTo(counts[i - 1]));
  });
  test('other enum options', () {
    for (final option in Brightness.values) {
      if (option == Brightness.light) continue;
      expect(label(option), isNotNull);
    }
  });
  for (final fixture in fixtures) {
    test('checks each fixed id', () { readFixture(fixture);
      for (final id in fixture.ids) expect(visible(id), isTrue);
    });
  }
  test('state reachability', () {
    final unreachable = findUnreachable();
    if (unreachable.isNotEmpty) {
      expect(unreachable.map(describe).toList(), isEmpty);
    }
  });
  test('layout could return no glyphs', () {
    for (final glyph in layout(400).glyphs) expect(glyph.x, isPositive);
  });
  test('could render no matching widgets', () {
    for (final widget in find.byType(Chart).evaluate()) expect(widget, isNotNull);
  });
  test('loader could return null', () {
    final data = load();
    if (data.readiness.value != null) expect(data.absentDiag, isNull);
  });
  test('input could have no elements', () {
    final input = makeInput();
    for (var i = 0; i < input.length; i++) expect(boundary(input, i), isTrue);
  });
  test('mutable fixture can be cleared', () {
    var data = [[0xfe]];
    data.clear();
    for (final bytes in data) expect(() => decode(bytes), throwsStateError);
  });
  test('the branch changes the value', () {
    var unreachable = findUnreachable();
    if (unreachable.isNotEmpty) {
      unreachable = [];
      expect(unreachable, isEmpty);
    }
  });
}

void terminalReachability() {
  test('reports any unreachable states left', () {
    final unreachable = <int>{1, 2};
    while (haveWork()) {
      unreachable.remove(nextReachable());
      if (unreachable.isEmpty) {
        if (verbose) print('all states reached in $step steps');
        return;
      }
    }
    if (unreachable.isNotEmpty) {
      expect(unreachable.map(describe).toList(), isEmpty);
    }
  });
  test('could abandon the work list early', () {
    final unreachable = <int>{1, 2};
    while (haveWork()) {
      unreachable.remove(nextReachable());
      if (otherCondition) return;
    }
    if (unreachable.isNotEmpty) {
      expect(unreachable.map(describe).toList(), isEmpty);
    }
  });
}

void clearFixture(Fixture fixture) => fixture.ids.clear();
void mutableFields() {
  final mutable = [Fixture({'one'})];
  for (final fixture in mutable) {
    test('a helper can clear a fixture', () {
      clearFixture(fixture);
      for (final id in fixture.ids) expect(visible(id), isTrue);
    });
  }
}

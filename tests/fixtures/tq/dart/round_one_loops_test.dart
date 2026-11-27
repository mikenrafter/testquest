import 'package:test/test.dart';

const _patterns = {'a': [1], 'b': [7]};
const _widths = [80, 120];
const _start = -300.0;
const _limit = 1100.0;
enum Mode { clock, timer }

void main() {
  // why: score_layout_test.dart:379 uses two fixed fixtures.
  test('fixed inputs', () {
    for (final width in _widths) {
      for (final pattern in _patterns.entries) {
        for (final n in {1: 7}.values) { expect(layout(pattern.value, width), n); }
      }
    }
  });
  // why: time_buzz_encoder_test.dart:135 and the geometry sweeps use fixed bounds.
  test('fixed numeric range and enum', () {
    for (var y = _start; y <= _limit; y += 20) {
      for (final mode in Mode.values) { expect(encode(y, mode), isPositive); }
    }
  });
  // why: shift_laws_test.dart:125 can iterate no findings even with a fixed fixture.
  test('loop over result', () {
    for (final pattern in _patterns.values) {
      for (final finding in score(pattern).findings) { expect(finding.score, isPositive); }
    }
  });
  test('empty fixed inputs', () {
    for (final p in const <String, int>{}.entries) { expect(p.value, 7); }
  });
  // why: args_test.dart:37 uses a top-level const declared after main.
  test('forward const fixture', () {
    for (final n in _later) { expect(encode(n), 7); }
  });
}

class Level {
  const Level();
  bool operator ==(Object other) => other is Level;
  int get hashCode => 1;
}

void equality() {
  // why: the Level test at logging_test.dart:15 uses the overloaded operator.
  test('comparison operators', () {
    final level = Level();
    expect(level == level, isTrue);
  });
  // why: capabilities_test.dart:53 compares two separately built values.
  test('value equality and hashCode', () {
    expect(Level(), Level());
    expect(Level().hashCode, Level().hashCode);
  });
}

const _later = [2, 5];

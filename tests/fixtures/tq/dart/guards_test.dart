import 'package:test/test.dart';

void main() {
  test('guard', () { if (enabled) expect(work(), 7); });
  test('branches', () { if (enabled) expect(work(), 7); else expect(other(), 9); });
  test('unknown loop', () { for (final item in items) { expect(item, isPositive); } });
  test('guaranteed loop', () { for (final item in <int>[7]) { expect(item, isPositive); } });
  test('collection if', () { for (final item in [if (enabled) 7]) { expect(item, isPositive); } });
  test('early exit', () { if (!enabled) return; expect(work(), 7); });
  test('break before check', () { for (final item in [7]) { if (enabled) break; expect(item, 7); } });
  test('do once', () { do { expect(work(), 7); } while (enabled); });
  test('short circuit', () { enabled && checkResult(); });
  test('condition executes its own check', () { if (checkResult()) work(); });
  test('statically true branch', () { if (true) expect(work(), 7); });
  test('counting loop starts below the bound', () { for (var i = 0; i < 1; i++) expect(i, 1); });
  test('counting loop starts at the bound', () { for (var i = 1; i < 1; i++) expect(i, 1); });
  test('finally checks on both exits', () {
    try { if (enabled) return; } finally { expect(work(), 7); }
  });
}
bool checkResult() { expect(work(), 7); return true; }

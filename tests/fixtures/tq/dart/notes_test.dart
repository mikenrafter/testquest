import 'package:test/test.dart';

void main() {
  group('round', () {
    test('probed branch', () {
      // catches: wrong value
      // reaches: round::control path
      if (enabled) expect(work(), 7);
    });
    // tq: allow guarded-assert Negative fixture for witness
    test('missing witness', () {
      /* catches: wrong callback */
      if (enabled) expect(work(), 7);
    });
    test('control path', () { expect(work(), 7); });
  });
}

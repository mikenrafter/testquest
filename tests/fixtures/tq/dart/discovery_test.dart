import 'package:test/test.dart' as t;
import 'package:flutter_test/flutter_test.dart';

void finish() { expect(work(), 7); }
void main() {
  t.group('suite', () {
    setUp(() { expect(work(), 7); });
    t.test('setup does not count', () {});
    t.test('skipped', () {}, skip: 'pending platform');
    t.group('skipped suite', () { test('ignored child', () {}); }, skip: true);
    t.test('enabled', () {}, skip: false);
    test('tearoff', finish);
    testWidgets('widgets', (tester) async {
      await tester.pump();
      expect(find.text('ready'), findsOneWidget);
    });
    test('throws', () => expect(work, throwsA(anything)));
  });
}

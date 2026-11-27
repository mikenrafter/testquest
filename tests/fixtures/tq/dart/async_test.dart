import 'package:test/test.dart';

void main() {
  test('bare expectLater', () { expectLater(work(), completion(7)); });
  test('returned expectLater', () => expectLater(work(), completion(7)));
  test('await unrelated work', () async { expectLater(work(), completion(7)); await cleanup(); });
  test('await matcher', () async { await expectLater(work(), throwsA(anything)); });
  test('detached callback', () async { work().then((value) => expect(value, 7)); });
  test('await callback', () async { await work().then((value) => expect(value, 7)); });
  test('detached future', () { Future<void>(() { expect(work(), 7); }); });
  test('await saved future', () async {
    final pending = Future<void>(() { expect(work(), 7); });
    await pending;
  });
  test('join waits', () async { await Future.wait([expectLater(work(), completion(7))]); });
  test('raw list does not wait', () async { await [expectLater(work(), completion(7))]; });
  test('microtask awaited', () async { await Future.microtask(() => expect(work(), 7)); });
  test('detached named future', () { delayedCheck(); });
  test('awaited named future', () async { await delayedCheck(); });
  test('await Future.value forwards the nested future', () async {
    await Future.value(expectLater(work(), completion(7)));
  });
}
Future<void> delayedCheck() => expectLater(work(), completion(7));

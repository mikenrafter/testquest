import 'dart:async';

import 'package:dart_h4/sql.dart';
import 'package:mockingjay/mockingjay.dart';
import 'package:test/test.dart';

import 'utils.dart';

class MockObs extends Mock {}

void main() {
  late SqlEngine engine;
  late List<MockObs> observers;
  late List<MockObs> emptyObservers;

  setUp(() {
    engine = SqlEngine();
    observers = [MockObs(), MockObs()];
    emptyObservers = [];
  });

  test('mockingjay verify called', () {
    final obs = MockObs();
    runAction(obs);
    verify(() => obs.onUndo()).called(1);
  });

  test('extension expectError helper', () {
    engine.analyze('SELECT (1, 2)').expectError('(1, 2)');
  });

  test('extension expectNoError helper', () {
    engine.analyze('SELECT 1').expectNoError();
  });

  test('setup built list loop', () {
    for (final obs in observers) {
      verify(() => obs.onEvent()).called(1);
    }
  });

  test('const list indexed loop', () {
    const times = [100, 200, 300];
    for (final (index, time) in times.indexed) {
      expect(index, lessThan(time));
    }
  });

  test('exclusively and Zone.run callbacks', () async {
    final db = TodoDb();
    final parent = Zone.current;
    await db.exclusively(() async {
      await parent.run(() async {
        await expectLater(Future.value(1), completion(1));
      });
    });
  });

  // --- Negative controls (must STILL flag) ---
  test('empty extension helper is hollow', () {
    engine.analyze('SELECT 1').expectNothing();
  });

  test('empty setup list loop is guarded', () {
    for (final obs in emptyObservers) {
      verify(() => obs.onEvent()).called(1);
    }
  });

  test('query result loop without count is guarded', () {
    for (final row in queryRows()) {
      expect(row, isNotNull);
    }
  });

  test('absence right after close lacks control', () {
    final calls = <String>[];
    final obj = makeStream(onEmit: calls.add);
    obj.close();
    expect(calls, isEmpty);
  });
}

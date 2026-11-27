import 'dart:io';

import 'package:dart_holdout/verify_helpers.dart';
import 'package:drift_sqlite/schema_verifier.dart';
import 'package:mocktail/mocktail.dart' as mocktail;
import 'package:test/test.dart';
import 'package:test_descriptor/test_descriptor.dart' as d;

class Permutations<T> {
  Permutations(this.r, this.items);
  final int r;
  final List<T> items;
  Iterable<List<T>> call() => [items];
}

void main() {
  test('future wait map over literal list', () async {
    await Future.wait(
      ['/a', '/b', '/c'].map((url) async {
        final res = await fetch(url);
        expect(res.path, url);
      }),
    );
  });

  test('const string separators loop', () {
    const String separators = '\t\n\r()<>';
    for (int i = 0; i < separators.length; i++) {
      expect(isInvalid(separators.substring(i, i + 1)), isTrue);
    }
  });

  test('top-level final matrix declared after main', () {
    for (final entry in laterMatrix) {
      expect(entry, isPositive);
    }
  });

  Future<void> runListCheck(List<Matcher> expectations) async {
    final result = buildQuery();
    for (final e in expectations) {
      expect(result, e);
    }
  }

  test('helper passed literal matcher list', () {
    return runListCheck([contains('SELECT'), contains('FROM')]);
  });

  test('permutations over fixed literal list', () {
    final perm = Permutations(2, ['a', 'b']);
    for (final p in perm()) {
      expect(resolve(p), isNotEmpty);
    }
  });

  test('if fail is check', () {
    final invalid = findInvalid();
    if (invalid.isNotEmpty) {
      fail('Unexpected invalid items: $invalid');
    }
  });

  void testFormat(String input, {String? expectedOutput}) {
    final parsed = parseInput(input);
    if (parsed.errors.isNotEmpty) {
      fail('Parse error');
    }
    final formatted = parsed.toSql();
    if (expectedOutput != null) {
      expect(formatted, expectedOutput);
    } else {
      final reparsed = parseInput(formatted);
      try {
        enforceAstEqual(parsed, reparsed);
      } catch (e) {
        fail('Not equal: $e');
      }
    }
  }

  test('both branches check in testFormat', () {
    testFormat('SELECT 1');
  });

  test('HttpOverrides runWithHttpOverrides', () async {
    await HttpOverrides.runWithHttpOverrides(
      () async {
        await expectLater(fetch('/x'), throwsException);
      },
      MyHttpOverrides(),
    );
  });

  test('drift testWithDataIntegrity', () async {
    await verifier.testWithDataIntegrity(
      oldVersion: 1,
      newVersion: 2,
      createItems: (batch, oldDb) {},
      validateItems: (newDb) async {
        expect(await newDb.selectAll(), isNotEmpty);
      },
    );
  });

  test('mocktail verify called', () {
    final obs = MockObs();
    runSubject(obs);
    mocktail.verify(() => obs.onCreate()).called(1);
  });

  test('riverpod verifyOnly getter helper', () {
    final listener = MockListener();
    readProvider(listener);
    verifyOnly(listener, listener());
  });

  test('test_descriptor validate', () async {
    await d.file('out.dart', 'void main() {}').validate();
  });

  test('enforce helper check', () {
    enforceAstEqual(parseInput('a'), parseInput('a'));
  });

  // --- Negative controls (must STILL flag) ---
  test('guarded if fail inside dynamic loop', () {
    for (final item in fetchDynamic()) {
      if (item < 0) fail('negative');
    }
  });

  test('empty test body', () {});

  void enforceNothing() {}

  test('empty enforce helper is hollow', () {
    enforceNothing();
  });

  test('absence with verifyNever still lacks control', () {
    final changes = <int>[];
    final obs = MockObs();
    createCubit(onChange: changes.add);
    expect(changes, isEmpty);
    mocktail.verifyNever(() => obs.onChange());
  });
}

final laterMatrix = [1, 2, 3];

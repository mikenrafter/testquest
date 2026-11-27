import 'package:test/test.dart';

void main() {
  test('same value', () => expect(value, equals(value)));
  test('constant predicate', () => expect(2 < 4, isTrue));
  test('constant mismatch', () => expect(2 + 2, 9));
  test('repeatable mapping', () => expect(calc(), calc()));
  test('runs once', () => expect(calc(), calc()));
  test('negated self', () => expect(value != value, isTrue));
  test('matcher type', () => expect(result, isA<List<int>>()));
  test('dynamic matcher', () => expect(value, contains(value)));
  test('boolean literal', () => expect(true, isTrue));
  test('two immutable aliases', () {
    final result = work();
    final alias = result;
    expect(alias, equals(result));
  });
  test('fixed mismatch', () => expect(2, 9));
  test('bool and int stay distinct', () => expect(1, true));
}

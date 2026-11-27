import 'package:test/test.dart';

void main() {
  test('recorded events missing control', () {
    final events = <String>[];
    subscribe(events.add);
    expect(events, isEmpty);
  });
  test('same recorder control', () {
    final events = <String>[];
    subscribe(events.add);
    expect(events.length, 0);
    publish();
    expect(events, isNotEmpty);
  });
  test('wrong recorder control', () {
    final events = <String>[];
    final logs = <String>[];
    subscribe(events.add);
    subscribe(logs.add);
    expect(events, equals([]));
    expect(logs, isNotEmpty);
  });
  test('conditional positive control', () {
    final events = <String>[];
    subscribe(events.add);
    expect(events.isEmpty, isTrue);
    if (enabled) expect(events, hasLength(1));
  });
  test('empty output value', () {
    final names = lookupNames();
    expect(names, isEmpty);
  });
  test('shadowed recorder cannot control the original', () {
    final events = <String>[];
    subscribe(events.add);
    expect(events, isEmpty);
    {
      final events = <String>[];
      subscribe(events.add);
      expect(events, isNotEmpty);
    }
  });
  test('control through an immutable alias', () {
    final events = <String>[];
    final alias = events;
    subscribe(alias.add);
    expect(events, isEmpty);
    publish();
    expect(alias, isNotEmpty);
  });
}

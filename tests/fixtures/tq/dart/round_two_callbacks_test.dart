import 'dart:async';
import 'package:test/test.dart';
import 'round_two_platform.dart';

void eachQueue(void Function(List<int>) callback) {
  group('queue shapes', () {
    test('with contiguous elements', () => callback([3]));
    test('with an internal gap', () => callback([1, 3]));
  });
}

void clientTest(String name, Future<void> Function(int) func) {
  return test(name, () {
    return func(clientPort());
  });
}

void widgetCase(String name, Future<void> Function(dynamic) body) =>
    testWidgets(name, (t) async {
      await body(t);
      await t.pump();
    });

void mixed(void Function() body) {
  test('one caller has no check', () => body());
}

void main() {
  eachQueue((queue) => expect(queue.last, 3));
  eachQueue((queue) { for (var i = 0; i < queueCapacity - 1; i++) expect(queue.first, isPositive); });
  clientTest('client handshake', (port) async => expect(port, isPositive));
  widgetCase('collapsed status', (t) async => expect(t.state, 'collapsed'));
  mixed(() => expect(reading(), 3));
  mixed(() => reading());

  test('sync platform', () {
    final platform = PlatformProbe.from('fake').copyWith();
    platform.run(() => expect(currentPlatform(), 'fake'));
  });
  test('async platform', () async {
    final platform = PlatformProbe.from('fake').copyWith();
    await platform.run(() async => expect(await currentPlatform(), 'fake'));
  });
  test('detached async platform', () {
    final platform = PlatformProbe.from('fake').copyWith();
    platform.run(() async => expect(await currentPlatform(), 'fake'));
  });
  test('conditional runner', () async {
    final platform = PlatformProbe.from('fake').copyWith();
    await platform.maybeRun(() async => expect(await currentPlatform(), 'fake'));
  });

  test('shift helper', () {
    final run = ShiftRun();
    run.expectNoErrors('sleep detail open');
  });
  test('worker decoding helper', () {
    p25ExpectDecodedInWorker(audit());
  });

  group('original operation completes', () {
    late void Function(int) onValue;
    setUp(() => onValue = expectAsync1((_) {}, count: 0));
    void runThen() { operation().then(onValue); }
    test('canceled operation', () {
      runThen();
      completer.complete(1);
    });
    test('callback was replaced', () {
      onValue = (_) {};
      runThen();
      completer.complete(1);
    });
    test('callback was not installed', () => completer.complete(1));
  });
  test('smoke without checking dispatch', () async => await runner.run(args));
  clientTest('ping with no explicit check', (_) async => await ping());
}

const queueCapacity = 16;

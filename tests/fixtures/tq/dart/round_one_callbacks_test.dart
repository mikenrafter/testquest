import 'dart:io' as io;

import 'package:fake_async/fake_async.dart';
import 'package:mockito/mockito.dart' as mocks;
import 'package:test/test.dart';

import 'round_one_support.dart' as support;
import 'package:dart_fixtures/support.dart' as fixtures;

void main() {
  // why: app_state_sync_dispose_test.dart:27 passes the body through an imported factory.
  test('resolved factory runs its callback', support.syncCase((n) async {
    expect(read(), n);
  }));
  test('resolved factory with no check', support.syncCase((n) async { work(n); })); // finding
  test('opaque factory', missingFactory(() {}));
  // why: restartable_timer_test.dart:60 and alarm_slot_probe_test.dart:308 run fake time synchronously.
  test('synchronous runners and local helper', () { fakeAsync((clock) => expectRefused()); });
  test('fake time method', () { FakeAsync().run((clock) => expect(read(), 7)); });
  // why: parse_test.dart:503 and ping_handler_test.dart:35 check through imported functions.
  test('helper', () { support.throwsFormat(parse); });
  test('mock calls', () { mocks.verify(writer.write(7)).called(1); });
  // why: cancelable_operation_test.dart:951 and stream_completer_test.dart:126 register callback checks.
  test('callback count', () { send(onCancel: expectAsync1((_) {}, count: 0)); });
  test('unreachable callback', () { empty.forEachIndexed(unreachable); });
  // why: server_test.dart:1522 waits for both local async functions.
  test('wait extension', () async {
    Future<void> client() async { await send(); expect(read(), 7); }
    Future<void> server() async { await start(); expect(read(), 9); }
    await [client(), server()].wait;
  });
  // why: subscription_stream_test.dart:227 can only finish when the expected error arrives.
  test('expected error finishes the test', () async {
    final done = Completer<void>();
    stream.listen((_) {}, onError: (_) => done.complete(), onDone: () => throw 'unexpected end');
    await done.future;
  });
  test('unrelated completion proves nothing', () async {
    final done = Completer<void>();
    stream.listen((_) {}, onError: (_) => done.complete());
    done.complete();
    await done.future;
  }); // finding at registration
  // why: same-package imports can bind a named callback and expose a fixed enum.
  test('named factory with a package URI', fixtures.withNamedBody(body: () async {
    for (final mode in fixtures.FixtureMode.values) { expect(use(mode), isPositive); }
  }));
  test('named factory with no check', fixtures.withNamedBody(body: () async { work(7); }));
  // why: cancelable_operation_test.dart:951 registers the check on an assignment RHS.
  test('assigned callback count', () { onCancel = expectAsync1((_) {}, count: 0); });
  // why: extensions_test.dart:189 uses a local callback whose only outcome is failure.
  test('local forbidden callback', () {
    Never unreachable(dynamic _) => fail('unexpected iteration');
    empty.forEach(unreachable);
  });
  // why: stream_completer_test.dart:126 waits for a completion tear-off.
  test('only the done callback completes the future', () async {
    final done = Completer<void>();
    stream.listen(null, onDone: done.complete);
    await done.future;
  });
  // why: client_test.dart:1698 bounds the wait for a failed connection to close.
  test('close completes within the bound', () async {
    await disconnect().timeout(const Duration(seconds: 2));
  });
  // why: io_test.dart:44 depends on the zone forwarding the test body.
  test('zoned factory', withLocalDirectory(() { expect(read(), 7); }));
  test('only the named completion callback finishes the test', () async {
    final finished = Completer<void>();
    save(onFinished: () => finished.complete());
    await finished.future;
  });
}

void expectRefused() => fakeAsync((clock) { expect(read(), 7); });

void Function() withLocalDirectory(void Function() body) => () => io.IOOverrides.runZoned(body);

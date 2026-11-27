import 'dart:async' as async;
import 'package:test/test.dart';

T runWith<T>(T Function() body) {
  state = true;
  return async.runZoned(body);
}

class PlatformProbe {
  PlatformProbe.from(String state);
  PlatformProbe copyWith() => PlatformProbe.from('new state');
  T run<T>(T Function() code) => runWith(code);
  T? maybeRun<T>(T Function() code) {
    if (enabled) return runWith(code);
    return null;
  }
}

class ShiftRun {
  void expectNoErrors(String name) => expect(errors(), isEmpty, reason: name);
}
void p25ExpectDecodedInWorker(dynamic audit) {
  expect(audit.worker, isNotEmpty);
}

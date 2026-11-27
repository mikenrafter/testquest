import 'package:test/test.dart';

Future<void> Function() syncCase(Future<void> Function(int) body) {
  return () async { await body(7); };
}

void throwsFormat(void Function() parse) => expect(parse, throwsFormatException);

Future<void> Function() withNamedBody({required Future<void> Function() body}) => () async {
  await body();
};

enum FixtureMode { clock, timer }

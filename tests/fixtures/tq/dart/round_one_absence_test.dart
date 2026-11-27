import 'package:test/test.dart';

final spinner = RegExp(r'Spinner\(');
final refresh = RegExp(r'RefreshIndicator\(');

void main() {
  // why: stream_queue_test.dart:613 checks the same recorder after delivery.
  test('before and after', () async {
    final values = <int>[];
    stream.listen(values.add);
    expect(values, isEmpty);
    await deliver();
    expect(values, [1, 2, 3]);
  });
  // why: logging_test.dart:754 shares the onLevelChanged observer with a sibling.
  test('level changes observed', () {
    final levels = <int>[];
    root.onLevelChanged.listen(levels.add);
    changeLevel();
    expect(levels, hasLength(2));
  });
  test('same level does not notify', () {
    final noChanges = <int>[];
    root.onLevelChanged.listen(noChanges.add);
    keepLevel();
    expect(noChanges, isEmpty);
  });
  test('other stream is unproven', () {
    final levels = <int>[];
    root.onOtherChanged.listen(levels.add);
    expect(levels, isEmpty);
  });
  // why: annotation_layout_laws_test.dart:1243 applies the threshold inline.
  test('test owns threshold', () {
    final weak = <String>[];
    expect(registry, isNotEmpty);
    for (final law in registry) {
      final got = law.measure();
      if (got['seen']! < 5) weak.add(law.name);
    }
    expect(weak, isEmpty);
  });
  // why: ui2_spinner_status_guard_test.dart:64 checks the regex on a positive sample.
  test('spinner detector', () { expect(spinner.hasMatch('Spinner()'), isTrue); });
  test('no spinners', () {
    final hits = <String>[];
    for (final file in dartFilesIn('lib')) {
      for (final m in spinner.allMatches(file.readAsStringSync())) { hits.add(file.path); }
    }
    expect(hits, isEmpty);
  });
  test('no refresh indicators', () {
    final hits = <String>[];
    for (final file in dartFilesIn('lib')) {
      for (final m in refresh.allMatches(file.readAsStringSync())) { hits.add(file.path); }
    }
    expect(hits, isEmpty);
  });
  // why: ecg_wording_test.dart:179 depends on an untested detector branch.
  test('wording scan', () {
    final hits = <String>[];
    for (final text in texts) {
      final bad = banned(text);
      if (bad.isNotEmpty) hits.add(text);
    }
    expect(hits, isEmpty);
  });
  // why: ui2_chart_scrub_guard_test.dart:56 checks the same matches feed the scan.
  test('scan counts matches before filtering', () {
    final hits = <String>[];
    var total = 0;
    for (final m in RegExp(r'ChartPainter').allMatches(code)) {
      total++;
      if (!enclosed(m)) hits.add(m.group(0)!);
    }
    expect(total, greaterThan(30));
    expect(hits, isEmpty);
  });
  // why: flow_control_test.dart:401 drops decode errors in the recorder.
  test('listener silently ignores decode errors', () {
    final goaways = <GoawayFrame>[];
    reader.listen((frame) {
      if (frame is GoawayFrame) goaways.add(frame);
    }, onError: (_) {});
    expect(goaways, isEmpty);
  });
  // why: p22_fix_round1_test.dart:82 checks both mutually exclusive success results.
  test('two reads succeeded', () async {
    final done = <String, String>{};
    final failed = <Object>[];
    final a = store.read('a').then((r) => done['a'] = r, onError: failed.add);
    final b = store.read('b').then((r) => done['b'] = r, onError: failed.add);
    await Future.wait([a, b]);
    expect(failed, isEmpty);
    expect(tag(done['a']!), 'A');
    expect(tag(done['b']!), 'B');
  });
  test('only one read checked', () async {
    final done = <String, String>{};
    final failed = <Object>[];
    final a = store.read('a').then((r) => done['a'] = r, onError: failed.add);
    final b = store.read('b').then((r) => done['b'] = r, onError: failed.add);
    await Future.wait([a, b]);
    expect(failed, isEmpty);
    expect(tag(done['a']!), 'A');
  });
}

List<String> banned(String text) => RegExp(r'banned').allMatches(text).map((m) => m[0]!).toList();

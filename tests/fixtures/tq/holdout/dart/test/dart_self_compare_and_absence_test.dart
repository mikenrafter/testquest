import 'package:mockito/mockito.dart';
import 'package:test/test.dart';

void main() {
  test('before-after snapshot across await', () async {
    final controller = makeController();
    final zoomBefore = controller.camera.zoom;
    await pumpFrame();
    expect(controller.camera.zoom, equals(zoomBefore));
  });

  test('before-after snapshot across mutation', () {
    final devtool = makeDevtool();
    final initialFrame = devtool.frames.single;
    devtool.increment();
    flushMicrotasks();
    expect(devtool.frames.single, same(initialFrame));
  });

  test('caches the provider per value', () {
    final family = makeFamily();
    expect(family(42), family(42));
  });

  test('Caches dependencies', () {
    final provider = makeProvider();
    expect(provider.dependencies, same(provider.dependencies));
  });

  test('parallel sink proves fill', () async {
    final client = MockClient();
    final requests = <Object>{};
    final requestsReferences = <WeakReference<Object>>{};
    when(client.openUrl(any)).thenAnswer((_) async {
      final req = makeReq();
      requests.add(req);
      requestsReferences.add(WeakReference(req));
      return req;
    });
    await runAndCancel(client);
    expect(requests, isEmpty);
    expect(requestsReferences, hasLength(2));
  });

  // --- Negative controls (must STILL flag) ---
  test('literal self compare', () {
    final deserialized = decode('x');
    expect(deserialized, equals(deserialized));
  });

  test('alias self compare without intervening action', () {
    final db = openDb();
    final ref1 = db.todosTable;
    final ref2 = db.todosTable;
    expect(ref1.hashCode, equals(ref2.hashCode));
  });

  test('separate callbacks do not prove fill', () async {
    final requests = <Object>[];
    final other = <Object>[];
    onReq((r) => requests.add(r));
    onOther((r) => other.add(r));
    expect(requests, isEmpty);
    expect(other, hasLength(1));
  });

  test('conditional write in same callback does not prove fill', () async {
    final client = MockClient();
    final requests = <Object>{};
    final refs = <Object>{};
    when(client.openUrl(any)).thenAnswer((_) async {
      final req = makeReq();
      if (shouldRecord()) {
        requests.add(req);
      }
      refs.add(req);
      return req;
    });
    expect(requests, isEmpty);
    expect(refs, hasLength(1));
  });
}

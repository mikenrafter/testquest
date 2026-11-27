import 'package:dart_h4/sql.dart';
import 'package:test/test.dart';

extension ExpectErrors on AnalysisContext {
  void expectError(String lexeme) {
    expect(errors, isNotEmpty);
  }

  void expectNoError() {
    expect(errors, isEmpty);
  }

  void expectNothing() {}
}

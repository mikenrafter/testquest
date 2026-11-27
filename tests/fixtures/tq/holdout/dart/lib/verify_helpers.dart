import 'package:mockito/mockito.dart';

typedef VerifyOnly = VerificationResult Function<ResT>(Mock mock, ResT matching);

VerifyOnly get verifyOnly {
  final verification = verify;
  return <ResT>(mock, invocation) {
    final result = verification(invocation);
    result.called(1);
    verifyNoMoreInteractions(mock);
    return result;
  };
}

class NotEqualException implements Exception {
  final String message;
  NotEqualException(this.message);
}

class AstChecker {
  void visit(Object b) {
    if (b.toString().isEmpty) {
      throw NotEqualException('empty');
    }
  }
}

void enforceAstEqual(Object a, Object b) {
  AstChecker().visit(b);
}

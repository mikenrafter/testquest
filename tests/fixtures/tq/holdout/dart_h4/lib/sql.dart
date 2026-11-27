class SqlEngine {
  AnalysisContext analyze(String sql) => AnalysisContext();
}

class AnalysisContext {
  final List<Object> errors = [];
}

class TodoDb {
  Future<T> exclusively<T>(Future<T> Function() action) async {
    return await action();
  }
}

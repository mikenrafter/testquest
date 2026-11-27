package okio.fakefilesystem

class FakeFileSystem {
    private val openFiles = mutableListOf<String>()

    fun sink(path: String): FakeSink {
        openFiles.add(path)
        return FakeSink(path, openFiles)
    }

    fun checkNoOpenFiles() {
        val firstOpenFile = openFiles.firstOrNull() ?: return
        throw IllegalStateException("expected 0 open files, but found: $firstOpenFile")
    }
}

class FakeSink(private val path: String, private val openFiles: MutableList<String>) {
    fun close() { openFiles.remove(path) }
}

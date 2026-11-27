package example

class CapturingTree {
    companion object {
        inline fun <T> capture(block: (CapturingTree) -> T): T {
            val tree = CapturingTree()
            plant(tree)
            try {
                return block(tree)
            } finally {
                uproot(tree)
            }
        }
    }
}

class Recorders {
    @Test void separate() {
        verify(other).send(probe);
        verify(sender, never()).send(message); // absence
    }
    @Test void witnessed() {
        verify(sender, times(1)).send(probe);
        verify(sender, times(0)).send(message);
    }
    @Test void guarded() {
        if (enabled()) verify(sender).send(probe);
        verifyNoInteractions(sender); // absence
    }
}

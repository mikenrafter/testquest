class RecorderControlsJava {
    private final Sender session = mock(Sender.class);
    private final Sender player = mock(Sender.class);

    @Test void leavesPlayingAlone() {
        verify(session, never()).release();
        verify(player, never()).release();
    }

    @Test void releasesWhenIdle() {
        verify(session, times(1)).release();
    }
}

package org.example.api;

public final class ClassMode {
    public static final ClassMode GET = new ClassMode();
    public static final ClassMode POST = new ClassMode();
    private static final ClassMode[] ALL = {GET, POST};

    public static ClassMode[] values() {
        return ALL;
    }
}

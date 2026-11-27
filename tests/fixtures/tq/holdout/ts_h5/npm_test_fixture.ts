import { test as _test } from '@playwright/test';

export const test = _test.extend<{ exec: (cmd: string, opts?: { expectToExitWithError?: boolean }) => Promise<void> }>({
  exec: async ({}, use) => {
    await use(async (_cmd: string, opts?: { expectToExitWithError?: boolean }) => {
      if (!opts?.expectToExitWithError) {
        throw new Error('command failed');
      }
    });
  },
});

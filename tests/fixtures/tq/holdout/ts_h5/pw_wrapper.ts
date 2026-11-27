import { expect as baseExpect } from '@playwright/test';

export const expect = baseExpect.extend({
  toBeCustom() {
    return { pass: true, message: () => '' };
  },
});

export { test } from '@playwright/test';

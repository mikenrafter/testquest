import { test } from '@playwright/test';
import { expect } from './pw_cjs_reexport';

test('pw cjs expect', () => {
  expect(42).toBe(42);
});

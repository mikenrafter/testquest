import { test, expect } from './pw_reexport';

test('pw wrapper expect', () => {
  expect(1 + 1).toBe(2);
});

test('playwright waits', async ({ page }) => {
  await page.waitForSelector('.ready');
  await page.getByText('ok').waitFor();
});

test('real missing assert after waitForSelector', async ({ page }) => {
  await page.waitForSelector('#idle');
  await page.getByText('Hello');
});

test('real missing assert', async ({ page }) => {
  await page.getByText('Hello');
});

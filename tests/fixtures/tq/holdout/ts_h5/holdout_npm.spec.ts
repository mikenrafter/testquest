import { test } from './npm_test_fixture';

test('exec fixture runs and checks exit status', async ({ exec }) => {
  await exec('node --version');
});

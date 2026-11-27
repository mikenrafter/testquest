import { expect, expectTypeOf, assertType, test } from 'vitest';
import {
  assertEqual,
  makePng,
  makeView,
  untilBrowserLogAfter,
  untilConsoleLogAfter,
} from './vite_helpers';

declare function hasLog(): boolean;
declare function loadItems(): string[];
declare function getMsgs(): Array<{ role: string; content: Array<{ type: string }> }>;
declare function assertDefined<T>(v: T | undefined): asserts v is T;

test('type level expectTypeOf and assertType', () => {
  expectTypeOf<{ a: number }>().toEqualTypeOf<{ a: number }>();
  assertType<number>(1);
});

test('type level assertEqual', () => {
  assertEqual<string, string>(true);
});

test('type error comment', () => {
  // @ts-expect-error
  const bad: number = 'nope';
  void bad;
});

test('untilBrowserLogAfter wait', async () => {
  await untilBrowserLogAfter(() => {}, 'ready');
});

test('untilConsoleLogAfter callback', async () => {
  await untilConsoleLogAfter(
    () => {},
    ['boot'],
    (logs) => {
      expect(logs).toHaveLength(1);
    },
  );
});

test('untilConsoleLogAfter target array', async () => {
  await untilConsoleLogAfter(() => {}, ['ready'], true);
});

test('let array table', () => {
  let urls = ['https://example.com', 'http://localhost'];
  for (const u of urls) {
    expect(u.length).toBeGreaterThan(0);
  }
});

test('nested png loop', () => {
  const png = makePng(13, 17);
  for (let x1 = 0; x1 < png.width; x1++) {
    for (let x2 = x1; x2 < png.width; x2++) {
      expect(x2 >= x1).toBe(true);
    }
  }
});

test('does not update loop', () => {
  while (Date.now() < 1000) {
    if (hasLog()) {
      throw new Error('updated');
    }
  }
});

test('real unguarded loop', () => {
  const items = loadItems();
  for (const item of items) {
    expect(item).toBeTruthy();
  }
});

test('object keys baseline', () => {
  const box: any = { items: {} };
  expect(Object.keys(box.items)).toHaveLength(0);
  box.items.foo = 1;
  expect(box.items.foo).toBe(1);
});

test('strips artifacts', () => {
  const messages = getMsgs();
  for (const msg of messages) {
    if (msg.role === 'assistant') {
      expect(msg.content.filter((p) => p.type === 'approval')).toHaveLength(0);
    }
  }
  const assistantMsg = messages.find((m) => m.role === 'assistant');
  assertDefined(assistantMsg);
  expect(assistantMsg.content.filter((p) => p.type === 'call')).toHaveLength(1);
});

test('last tool message', () => {
  const messages = getMsgs();
  for (const msg of messages) {
    if (msg.role === 'tool') {
      expect(msg.content.filter((p) => p.type === 'approval-response')).toHaveLength(0);
    }
  }
  const lastMsg = messages[messages.length - 1];
  expect(lastMsg.role).toBe('tool');
  if (lastMsg.role === 'tool') {
    expect(lastMsg.content.filter((p) => p.type === 'tool-result')).toHaveLength(1);
  }
});

test('both in role loop unguarded', () => {
  const messages = getMsgs();
  for (const msg of messages) {
    if (msg.role === 'assistant') {
      expect(msg.content.filter((p) => p.type === 'call')).toHaveLength(1);
      expect(msg.content.filter((p) => p.type === 'approval')).toHaveLength(0);
    }
  }
});

async function openLocalView() {
  return await makeView({ decorate: true });
}

test('real imported setup helper return value assigned', async () => {
  const v = await openLocalView();
  void v;
});

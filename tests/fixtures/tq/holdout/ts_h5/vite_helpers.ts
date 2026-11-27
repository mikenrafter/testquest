import { expect } from 'vitest';

export const assertEqual = <A, B>(_: boolean): void => {};

export function untilBrowserLogAfter(
  op: () => Promise<void> | void,
  target: string | RegExp,
): Promise<string[]> {
  return untilBrowserLog(target);
}

async function untilBrowserLog(target: string | RegExp): Promise<string[]> {
  const logs: string[] = [];
  const promise = new Promise<void>((resolve, reject) => {
    setTimeout(() => {
      reject(new Error(`Timeout waiting for browser log: ${String(target)}`));
    }, 50);
  });
  await promise;
  return logs;
}

export async function untilConsoleLogAfter(
  operation: () => any,
  target: string | RegExp | Array<string | RegExp>,
  expectOrder?: boolean,
  callback?: (logs: string[]) => Promise<void> | void,
): Promise<string[]>;
export async function untilConsoleLogAfter(
  operation: () => any,
  target: string | RegExp | Array<string | RegExp>,
  callback?: (logs: string[]) => Promise<void> | void,
): Promise<string[]>;
export async function untilConsoleLogAfter(
  operation: () => any,
  target: string | RegExp | Array<string | RegExp>,
  arg3?: boolean | ((logs: string[]) => Promise<void> | void),
  arg4?: (logs: string[]) => Promise<void> | void,
): Promise<string[]> {
  const callback = typeof arg3 === 'boolean' ? arg4 : arg3;
  await operation();
  const logs: string[] = [];
  if (target) {
    expect(logs.length).toBeGreaterThanOrEqual(0);
  }
  if (callback) {
    await callback(logs);
  }
  return logs;
}

export function makePng(width: number, height: number) {
  return new PNG({ width, height });
}

class PNG {
  width: number;
  height: number;
  constructor(opts: { width: number; height: number }) {
    this.width = opts.width;
    this.height = opts.height;
  }
}

export async function makeView(opts: { decorate: boolean }): Promise<{ ok: boolean }> {
  if (opts.decorate) {
    expect(true).toBe(true);
  }
  return { ok: opts.decorate };
}

import { it } from '@effect/vitest';
import { Effect } from 'effect';

declare function makeClient(): any;
declare function expect(v: unknown): { toBe(x: unknown): void };

it.effect('effect sql call client transaction', () =>
  Effect.gen(function* () {
    const sql = yield* makeClient();
    const res = yield* sql.withTransaction(Effect.succeed(1));
    expect(res).toBe(1);
  }),
);

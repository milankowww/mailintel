// SPDX-FileCopyrightText: Milan Pikula
// SPDX-License-Identifier: EUPL-1.2

import test from 'node:test';
import assert from 'node:assert/strict';
import { createMessageStars } from '../src/message-stars.js';

test('hydrates current stars, shares updates and preserves integer values', async () => {
  const calls = [];
  const store = createMessageStars(async (url, options) => {
    calls.push(url);
    if (!options) return { stars: [{ id: 7, star: 3 }] };
    return { id: 7, star: JSON.parse(options.body).star };
  });
  await Promise.all([store.load(), store.load()]);
  assert.equal(calls.length, 1);
  assert.equal(store.state.values[7], 3);
  await store.toggle(7);
  assert.equal(store.state.values[7], 0);
  await store.toggle(7);
  assert.equal(store.state.values[7], 1);
  assert.equal(store.state.revision, 2);
});

test('deduplicates concurrent toggles and leaves failed saves unchanged', async () => {
  let finish;
  const store = createMessageStars(async (_, options) => {
    if (!options) return { stars: [{ id: 7, star: 1 }] };
    await new Promise((resolve) => { finish = resolve; });
    throw new Error('offline');
  });
  await store.load();
  const first = store.toggle(7);
  await store.toggle(7);
  finish();
  await first;
  assert.equal(store.state.values[7], 1);
  assert.equal(store.state.pending[7], false);
  assert.match(store.state.errors[7], /try again/);
  assert.equal(store.state.revision, 0);
});

test('Starred view discards stale pages and immediately filters removed stars', async () => {
  const { readFileSync } = await import('node:fs');
  const { default: vm } = await import('node:vm');
  const source = readFileSync(new URL('../src/main.js', import.meta.url), 'utf8');
  const start = source.indexOf('const TriageView =');
  const end = source.indexOf('\nconst ', start + 1);
  const responses = [];
  const definition = vm.runInNewContext(`${source.slice(start, end)}\nTriageView`, {
    MailMessageList: {}, SummaryEntryItem: {},
    messageStars: { load: async () => {} },
    fetchJson: () => new Promise(resolve => responses.push(resolve)),
  });
  const state = { starredRequest: 0, starred: [], stars: { ready: true, values: { 1: 1, 2: 1 } } };
  const load = definition.methods.loadStarred.bind(state);
  const old = load();
  await new Promise(setImmediate);
  const latest = load();
  await new Promise(setImmediate);
  responses[1]({ messages: [{ id: 2 }], has_more: false });
  await latest;
  responses[0]({ messages: [{ id: 1 }], has_more: true });
  await old;
  assert.deepEqual(state.starred, [{ id: 2 }]);
  state.stars.values[2] = 0;
  assert.equal(definition.computed.visibleStarred.call(state).length, 0);
});

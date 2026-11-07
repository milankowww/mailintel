// SPDX-FileCopyrightText: Milan Pikula
// SPDX-License-Identifier: EUPL-1.2
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';

const source = readFileSync(new URL('../src/main.js', import.meta.url), 'utf8');
const component = source.slice(source.indexOf('const SummaryEntryItem = {'), source.indexOf('const TriageView = {'));
function setup(fetchJson) {
  const definition = vm.runInNewContext(`${component}\nSummaryEntryItem`, {
    fetchJson, MailMessageList: { props: ['items'], template: '<div v-for="item in items">{{ item.subject }}</div>' },
  });
  definition.components.VBtn = { template: '<button><slot /></button>' };
  const state = { ...definition.data(), entry: { message_ids: [101, 1, 102] } };
  for (const [name, method] of Object.entries(definition.methods)) state[name] = method.bind(state);
  for (const [name, getter] of Object.entries(definition.computed)) Object.defineProperty(state, name, { get: getter.bind(state) });
  return { definition, state };
}

test('missing reference preserves rendered messages and reopening retries', async () => {
  let missing = true;
  let calls = 0;
  const { definition, state } = setup(async url => {
    calls++;
    const id = Number(url.split('/').pop());
    if (id === 1 && missing) throw new Error('404');
    return { message: { id, subject: `Subject ${id}`, summary_text: 'Summary.' } };
  });
  await state.toggleMessages();
  assert.deepEqual(Array.from(state.messages, m => m.id), [101, 102]);
  assert.match(state.error, /1 of 3 messages/);
  assert.equal(state.loading, false);
  const html = await renderToString(createSSRApp({
    ...definition,
    data: () => ({ expanded: state.expanded, loading: state.loading, messages: state.messages, error: state.error }),
  }, { entry: state.entry, entryKey: 'sample' }));
  assert.match(html, /Subject 101/);
  assert.match(html, /Subject 102/);
  assert.match(html, /1 of 3 messages/);
  missing = false;
  await state.toggleMessages();
  await state.toggleMessages();
  assert.equal(state.messages.length, 3);
  assert.equal(state.error, '');
  await state.toggleMessages();
  await state.toggleMessages();
  assert.equal(calls, 6);
});

test('all failed references show an error instead of a loading placeholder', async () => {
  const { definition, state } = setup(async () => { throw new Error('Offline'); });
  await state.toggleMessages();
  assert.equal(state.messages.length, 0);
  assert.match(state.error, /3 of 3 messages/);
  assert.equal(state.loading, false);
  const html = await renderToString(createSSRApp({ ...definition,
    data: () => ({ expanded: true, loading: false, messages: [], error: state.error }),
  }, { entry: state.entry, entryKey: 'sample' }));
  assert.match(html, /3 of 3 messages/);
  assert.doesNotMatch(html, /Loading messages/);
});

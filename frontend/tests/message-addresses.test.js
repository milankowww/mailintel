// SPDX-FileCopyrightText: Milan Pikula
// SPDX-License-Identifier: EUPL-1.2
import test from 'node:test';
import assert from 'node:assert/strict';
import { createMessageAddresses } from '../src/message-addresses.js';

function instance(fetchJson, item = { id: 7 }) {
  const component = createMessageAddresses(fetchJson);
  const state = { ...component.data(), item };
  for (const [key, fn] of Object.entries(component.methods)) state[key] = fn.bind(state);
  for (const [key, fn] of Object.entries(component.computed)) Object.defineProperty(state, key, { get: fn.bind(state) });
  return { component, state };
}

test('loads full addresses on demand and caches the detail response', async () => {
  const calls = [];
  const { state } = instance(async url => {
    calls.push(url);
    return { message: { sender_name: 'Alice', sender_address: 'alice@example.com',
      recipients_json: ['Bob <bob@example.com>', 'carol@example.com'], cc_json: [] } };
  });
  assert.deepEqual(calls, []);
  state.toggle({ target: { open: false } });
  assert.deepEqual(calls, []);
  await state.load();
  assert.equal(state.sender, 'Alice <alice@example.com>');
  assert.equal(state.recipientText(state.addresses.recipients_json), 'Bob <bob@example.com>; carol@example.com');
  assert.equal(state.recipientText(state.addresses.cc_json), 'None listed');
  await state.load();
  assert.deepEqual(calls, ['/api/messages/7']);
});

test('uses existing detail fields and distinguishes missing legacy addresses from an empty list', async () => {
  const { state } = instance(() => { throw Error('Unexpected request'); }, {
    id: 8, sender_address: 'sender@example.com', recipients_json: [''], cc_json: [],
  });
  await state.load();
  assert.equal(state.sender, 'sender@example.com');
  assert.equal(state.recipientText(state.addresses.recipients_json), 'Unavailable');
  assert.equal(state.recipientText(undefined), 'Unavailable');
  assert.equal(state.recipientText([]), 'None listed');
});

test('allows retry after failure and discards results for a previous message', async () => {
  let calls = 0;
  let finish;
  const { component, state } = instance(async () => {
    if (++calls === 1) throw Error('Offline');
    return new Promise(resolve => { finish = resolve; });
  });
  await state.load();
  assert.match(state.error, /try again/);
  const pending = state.load();
  state.item = { id: 9 };
  component.watch.messageId.call(state);
  finish({ message: { sender_address: 'previous@example.com' } });
  await pending;
  assert.equal(state.detail, null);
  assert.equal(state.loading, false);
});

test('renders address text safely in a collapsed, keyboard-accessible disclosure', async () => {
  const { createSSRApp } = await import('vue');
  const { renderToString } = await import('@vue/server-renderer');
  const { default: config } = await import('../vite.config.js');
  const { readFileSync } = await import('node:fs');
  const { default: vm } = await import('node:vm');
  const Vue = await import('vue');
  const source = readFileSync(new URL('../src/message-addresses.js', import.meta.url), 'utf8');
  const transformed = config.plugins[0].transform(source, '/src/message-addresses.js');
  assert.ok(transformed, 'Production build must compile the address component');
  const code = transformed.code.replace('import * as Vue from "vue";', '').replace('export function', 'function');
  const factory = vm.runInNewContext(`${code}\ncreateMessageAddresses`, { Vue });
  const component = factory(() => { throw Error('Unexpected request'); });
  assert.equal(typeof component.render, 'function');
  assert.equal(component.template, undefined);
  const html = await renderToString(createSSRApp(component, { item: {
    id: 10, sender_name: '<script>alert(1)</script>', sender_address: 'sender@example.com',
    recipients_json: ['Alice <alice@example.com>', 'bob@example.com'], cc_json: ['copy@example.com'],
  } }));
  assert.match(html, /<details class="mail-addresses">/);
  assert.match(html, /<summary title="Show or hide sender and recipients">/);
  assert.match(html, /class="mail-addresses-sender"/);
  assert.match(html, /Alice &lt;alice@example.com&gt;; bob@example.com/);
  assert.match(html, /copy@example.com/);
  assert.ok(!html.includes('<script>'));
});

// SPDX-FileCopyrightText: Milan Pikula
// SPDX-License-Identifier: EUPL-1.2

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// Exercise the actual root component methods without mounting the application
// (which would start background polling and require browser authentication).
const source = readFileSync(new URL('../src/main.js', import.meta.url), 'utf8');
const component = source.slice(source.indexOf('const App = {'), source.indexOf('const vuetify ='));
function app(fetchJson) {
  const definition = vm.runInNewContext(`${component}\nApp`, {
    fetchJson, MailMessageItem: {}, SummaryEntryItem: {}, authState: {},
  });
  const instance = definition.data();
  for (const [name, method] of Object.entries(definition.methods)) instance[name] = method.bind(instance);
  instance.scheduleStatusPoll = () => {};
  instance.loadStatus = async () => {};
  return instance;
}

test('new search clears old evidence and clarification before its request returns', async () => {
  let rejectRequest;
  const state = app(() => new Promise((_, reject) => { rejectRequest = reject; }));
  state.chatCards = [{ tone: 'email-source', message: { message_id: 7 } }];
  state.pendingSearchClarification = { question: 'Old follow-up' };
  state.clarificationResponse = 'Old response';
  const pending = state.submitSmartSearch('New question');
  assert.equal(state.chatCards.some(card => card.tone === 'email-source'), false);
  assert.equal(state.pendingSearchClarification, null);
  assert.equal(state.clarificationResponse, '');
  rejectRequest(new Error('Request failed'));
  await pending;
  assert.equal(state.chatCards.some(card => card.tone === 'email-source'), false);
});

test('late poll cannot restore evidence after leaving and reopening the same job', async () => {
  let resolveRequest;
  const state = app(() => new Promise(resolve => { resolveRequest = resolve; }));
  state.chatLoading = true;
  state.currentSearchId = state.selectedSearchId = 12;
  let renders = 0;
  state.renderSmartSearchState = () => { renders++; };
  const pending = state.pollSmartSearch(12);
  state.searchViewVersion++;
  resolveRequest({ search: { status: 'succeeded', matching_sources: [{ id: 7 }] } });
  await pending;
  assert.equal(renders, 0);
});

// SPDX-FileCopyrightText: Milan Pikula
// SPDX-License-Identifier: EUPL-1.2

// Shared across every occurrence of an email, including saved search evidence.
export function createMessageStars(fetchJson, reactive = (value) => value) {
  const state = reactive({ values: {}, pending: {}, errors: {}, ready: false, revision: 0 });
  let loading;
  async function load() {
    if (state.ready) return;
    if (!loading) loading = fetchJson('/api/message-stars').then((data) => {
      state.values = Object.fromEntries(data.stars.map((row) => [row.id, row.star]));
      state.ready = true;
    }).finally(() => { loading = null; });
    return loading;
  }
  async function toggle(id) {
    if (state.pending[id]) return;
    state.pending[id] = true;
    state.errors[id] = '';
    try {
      await load();
      const data = await fetchJson(`/api/messages/${id}/star`, {
        method: 'PUT', body: JSON.stringify({ star: state.values[id] > 0 ? 0 : 1 }),
      });
      state.values[id] = data.star;
      state.revision++;
    } catch {
      state.errors[id] = 'Could not update star. Please try again.';
    } finally { state.pending[id] = false; }
  }
  return { state, load, toggle };
}

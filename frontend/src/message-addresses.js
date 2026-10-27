// SPDX-FileCopyrightText: Milan Pikula
// SPDX-License-Identifier: EUPL-1.2

export function createMessageAddresses(fetchJson) {
  return {
    props: {
      item: { type: Object, required: true },
      before: { type: String, default: '' },
      after: { type: String, default: '' },
    },
    data() {
      return { detail: null, loading: false, error: '', request: 0, open: false };
    },
    computed: {
      messageId() { return Number(this.item.message_id || this.item.id); },
      addresses() { return this.detail || this.item; },
      sender() {
        const { sender_name: name, sender_address: address } = this.addresses;
        return name && address ? `${name} <${address}>` : address || name || 'Unavailable';
      },
    },
    watch: {
      messageId() {
        this.request += 1;
        this.detail = null;
        this.loading = false;
        this.error = '';
        if (this.open) this.load();
      },
    },
    methods: {
      recipientText(values) {
        if (!Array.isArray(values)) return 'Unavailable';
        if (!values.length) return 'None listed';
        return values.filter(value => String(value).trim()).join('; ') || 'Unavailable';
      },
      toggle(event) {
        this.open = event.target.open;
        if (this.open) this.load();
      },
      async load() {
        if (this.loading || this.detail) return;
        if (Array.isArray(this.item.recipients_json) && Array.isArray(this.item.cc_json)) return;
        if (!Number.isSafeInteger(this.messageId) || this.messageId <= 0) {
          this.error = 'Address details are unavailable for this message.';
          return;
        }
        const request = ++this.request;
        this.loading = true;
        this.error = '';
        try {
          const data = await fetchJson(`/api/messages/${this.messageId}`);
          if (request === this.request) this.detail = data.message;
        } catch {
          if (request === this.request) this.error = 'Could not load addresses. Please try again.';
        } finally {
          if (request === this.request) this.loading = false;
        }
      },
    },
    template: `
      <details class="mail-addresses" @toggle="toggle">
        <summary title="Show or hide sender and recipients">
          <span v-if="before">{{ before }} | </span><span class="mail-addresses-sender">{{ item.sender_name || item.sender_address || 'From' }}</span><span v-if="after"> | {{ after }}</span>
        </summary>
        <p v-if="loading" role="status">Loading addresses…</p>
        <div v-else-if="error" role="alert">
          {{ error }} <button type="button" @click="load">Retry</button>
        </div>
        <dl v-else>
          <dt>From</dt><dd>{{ sender }}</dd>
          <dt>To</dt><dd>{{ recipientText(addresses.recipients_json) }}</dd>
          <dt>Cc</dt><dd>{{ recipientText(addresses.cc_json) }}</dd>
        </dl>
      </details>
    `,
  };
}

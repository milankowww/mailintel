// SPDX-FileCopyrightText: Milan Pikula
// SPDX-License-Identifier: EUPL-1.2

import { GoogleOAuthSetup } from "./google-oauth-setup.js";
import { createMessageStars } from "./message-stars.js";
import { createApp, reactive } from "vue";
import { matchingSources, searchFailureMessage } from "./smart-search-view.js";
import * as Vue from "vue";
import { createRouter, createWebHashHistory } from "vue-router";
import { createVuetify } from "vuetify";
import * as components from "vuetify/components";
import * as directives from "vuetify/directives";
import "vuetify/styles";
import "@mdi/font/css/materialdesignicons.css";
import "./style.css";

function readStoredApiToken() {
  try {
    return window.sessionStorage?.getItem("mailintel.apiToken") || "";
  } catch {
    return "";
  }
}

function storeApiToken(token) {
  try {
    if (token) {
      window.sessionStorage?.setItem("mailintel.apiToken", token);
    } else {
      window.sessionStorage?.removeItem("mailintel.apiToken");
    }
  } catch {
    // Some embedded browser contexts disable session storage.
  }
}

const authState = reactive({
  token: readStoredApiToken(),
  required: false,
  waiters: [],
});

function requestApiToken() {
  authState.required = true;
  return new Promise((resolve) => {
    authState.waiters.push(resolve);
  });
}

function completeApiToken(token) {
  authState.token = token;
  authState.required = false;
  storeApiToken(token);
  const waiters = authState.waiters.splice(0);
  waiters.forEach((resolve) => resolve(token));
}

async function fetchJson(url, options = {}) {
  const token = authState.token || readStoredApiToken();
  const headers = {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(options.headers || {}),
  };
  const response = await fetch(url, {
    ...options,
    headers,
  });
  if (response.status === 401) {
    authState.token = "";
    storeApiToken("");
    const nextToken = await requestApiToken();
    if (nextToken) {
      return fetchJson(url, options);
    }
  }
  if (!response.ok) {
    let detail = `Request failed: ${response.status}`;
    try {
      const body = await response.json();
      if (body.detail) {
        const message = typeof body.detail === "object"
          ? (body.detail.message || body.detail.code || JSON.stringify(body.detail))
          : body.detail;
        detail = `${detail} ${message}`;
      }
    } catch {
      // keep default
    }
    throw new Error(detail);
  }
  return response.json();
}

function parseCsv(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatDate(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return parsed.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function isoDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function addDays(date, days) {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

function addMonths(date, months) {
  const next = new Date(date);
  next.setMonth(next.getMonth() + months, 1);
  return next;
}

function startOfIsoWeek(date) {
  const next = new Date(date);
  const day = next.getDay() || 7;
  next.setDate(next.getDate() - day + 1);
  return next;
}

function startOfMonth(date) {
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

function parseIsoDate(value) {
  const [year, month, day] = String(value || "")
    .split("-")
    .map((part) => Number(part));
  if (!year || !month || !day) return new Date();
  return new Date(year, month - 1, day);
}

function formatDateOnly(value) {
  const parsed = typeof value === "string" ? parseIsoDate(value) : value;
  return parsed.toLocaleDateString([], {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function defaultAccountForm() {
  return {
    id: null,
    name: "",
    provider_type: "imap",
    password: "",
    secret_locator: "",
    auth_mode: "env",
    active: true,

    host: "",
    port: "",
    username: "",
    sync_window_days: "",
    ssl: true,
    ignore_tls_errors: false,
    endpoint: "",
  };
}

function defaultUserProfile() {
  return {
    name: "",
    role: "",
    organization: "",
    aliases: "",
    email_addresses: "",
    collaborators: "",
    communication_preferences: "",
    about: "",
  };
}

function defaultInterestProfile() {
  return {
    id: null,
    name: "",
    description: "",
    collaborators: "",
    signals: "",
    priority_weight: 1,
    dashboard_prominence: 1,
    summary_detail: "standard",
    watch_mode: "watch",
  };
}

const PRESET_CAPABILITIES = [
  {value: "mail_summary", title: "Mail summaries and interest matching", model: "text_model"},
  {value: "mail_embedding", title: "Mail embeddings", model: "embedding_model"},
  {value: "daily_summary", title: "Daily summaries", model: "text_model"},
  {value: "rollup_summary", title: "Weekly and monthly summaries", model: "text_model"},
  {value: "topic_label", title: "Topic labels", model: "text_model"},
  {value: "interval_embedding", title: "Summary embeddings", model: "embedding_model"},
  {value: "search_plan", title: "Search planning", model: "text_model"},
  {value: "search_embedding", title: "Search embeddings", model: "embedding_model"},
  {value: "search_compress", title: "Search evidence compression", model: "text_model"},
  {value: "search_answer", title: "Search answers", model: "text_model"},
];

function defaultPreset(existing = []) {
  const nextPriority = existing.length ? Math.max(...existing.map((preset) => Number(preset.priority || 0))) + 10 : 100;
  return {
    id: null,
    name: "",
    provider: "ollama",
    enabled: false,
    priority: nextPriority,
    auth_mode: "env",
    secret_locator: "",
    endpoint_url: "http://127.0.0.1:11434",
    text_model: "llama3.1",
    embedding_model: "nomic-embed-text",
    temperature: 0.2,
    text_context_window: 8192,
    embedding_context_window: 8192,
    max_output_tokens: 800,
    initial_timeout_seconds: null,
    streaming_timeout_seconds: null,
    concurrency: 1,
    capabilities: PRESET_CAPABILITIES.map(item => item.value),
    notes: "",
  };
}

const MESSAGE_DISPLAY_MODES = ["compact", "comfort", "full"];

const messageStars = createMessageStars(fetchJson, reactive);
const MessageStarButton = {
  props: { messageId: { type: Number, required: true } },
  data() { return { stars: messageStars.state }; },
  mounted() { messageStars.load().catch(() => {}); },
  methods: { toggle() { return messageStars.toggle(this.messageId); } },
  template: `
    <span>
      <v-btn size="x-small" variant="text"
        :icon="stars.values[messageId] > 0 ? 'mdi-star' : 'mdi-star-outline'"
        :color="stars.values[messageId] > 0 ? 'amber-darken-2' : undefined"
        :loading="!!stars.pending[messageId]"
        :aria-pressed="stars.values[messageId] > 0"
        :aria-label="stars.values[messageId] > 0 ? 'Unstar email' : 'Star email'"
        :title="stars.values[messageId] > 0 ? 'Unstar email' : 'Star email'"
        @click.stop="toggle" />
      <span v-if="stars.errors[messageId]" role="alert" class="text-error text-caption">{{ stars.errors[messageId] }}</span>
    </span>`,
};

const MailMessageItem = {
  components: { MessageStarButton },
  props: {
    item: { type: Object, required: true },
    displayMode: { type: String, default: "comfort" },
    scoreKey: { type: String, default: "" },
    scoreLabel: { type: String, default: "" },
    showReadState: { type: Boolean, default: true },
    showPreview: { type: Boolean, default: false },
    showInterestChips: { type: Boolean, default: false },
  },
  data() {
    return {
      overrideMode: "",
    };
  },
  computed: {
    effectiveMode() {
      return MESSAGE_DISPLAY_MODES.includes(this.overrideMode) ? this.overrideMode : this.normalizedDisplayMode(this.displayMode);
    },
    isCompact() {
      return this.effectiveMode === "compact";
    },
    isFull() {
      return this.effectiveMode === "full";
    },
    titleText() {
      return this.item.subject || this.item.title || "Untitled message";
    },
    compactSummary() {
      return this.item.summary_text || this.item.preview_text || this.item.snippet || this.item.profile_match_rationale || "No preview available yet.";
    },
    modeIcon() {
      return {
        compact: "mdi-format-list-bulleted",
        comfort: "mdi-card-text-outline",
        full: "mdi-text-box-outline",
      }[this.effectiveMode] || "mdi-card-text-outline";
    },
    modeTitle() {
      const next = this.nextMode(this.effectiveMode);
      return `Show ${next} view for this message`;
    },
  },
  methods: {
    normalizedDisplayMode(value) {
      return MESSAGE_DISPLAY_MODES.includes(value) ? value : "comfort";
    },
    nextMode(value) {
      const current = this.normalizedDisplayMode(value);
      const index = MESSAGE_DISPLAY_MODES.indexOf(current);
      return MESSAGE_DISPLAY_MODES[(index + 1) % MESSAGE_DISPLAY_MODES.length];
    },
    cycleMode() {
      this.overrideMode = this.nextMode(this.effectiveMode);
    },
    sourcePrefix(item) {
      const account = item.account_name || item.provider_mailbox_id || "";
      const mailbox = item.mailbox_name && item.mailbox_name !== item.account_name ? item.mailbox_name : "";
      if (account && mailbox) return `${account} / ${mailbox}`;
      return account || mailbox || "";
    },
    metaParts(item) {
      const parts = [];
      if (item.search_relevance === "supporting") parts.push("supporting evidence");
      if (item.search_relevance === "maybe_relevant") parts.push("possibly relevant");
      if (this.scoreKey && item[this.scoreKey] !== null && item[this.scoreKey] !== undefined) {
        parts.push(`${Number(item[this.scoreKey]).toFixed(2)} ${this.scoreLabel}`);
      }
      if (item.importance_score !== null && item.importance_score !== undefined) {
        parts.push(`${Number(item.importance_score || 0).toFixed(2)} importance`);
      }
      if (this.showReadState) parts.push(item.is_read ? "read" : "unread");
      if (item.sender_name || item.sender_address) parts.push(item.sender_name || item.sender_address);
      const received = formatDate(item.received_at || item.date);
      if (received) parts.push(received);
      if (item.has_embeddings) parts.push("searchable");
      return parts.join(" | ");
    },
    interestProfiles(item) {
      return Array.isArray(item.interest_profiles) ? item.interest_profiles : [];
    },
    hasPreview(item) {
      return Boolean(item.preview_text && item.preview_text !== item.summary_text);
    },
  },
  template: `
    <article class="mail-message" :class="['mail-message--' + effectiveMode, item.search_relevance ? 'mail-message--' + item.search_relevance : '']">
      <template v-if="isCompact">
        <div class="mail-message-row-main">
          <div class="mail-message-row-title">
            <span v-if="sourcePrefix(item)">{{ sourcePrefix(item) }}: </span>{{ titleText }}
          </div>
          <div class="mail-message-row-summary">{{ compactSummary }}</div>
        </div>
        <div class="mail-message-row-side">
          <span v-if="item.search_relevance" class="mail-message-evidence-label">{{ item.search_relevance === 'supporting' ? 'Supporting' : 'Possible' }}</span>
          <span v-if="scoreKey && item[scoreKey] !== null && item[scoreKey] !== undefined" class="mail-message-row-score">{{ Number(item[scoreKey]).toFixed(2) }}</span>
          <MessageStarButton :message-id="Number(item.message_id || item.id)" />
          <v-btn
            size="x-small"
            variant="text"
            :icon="modeIcon"
            class="mail-message-mode-btn"
            :title="modeTitle"
            :aria-label="modeTitle"
            @click="cycleMode"
          />
        </div>
      </template>
      <v-card v-else class="mail-card" variant="flat">
        <v-card-title class="mail-card-title-row text-subtitle-1 font-weight-bold">
          <div class="mail-card-title-text">
            <span v-if="sourcePrefix(item)">{{ sourcePrefix(item) }}: </span>{{ titleText }}
          </div>
          <MessageStarButton :message-id="Number(item.message_id || item.id)" />
          <v-btn
            size="x-small"
            variant="text"
            :icon="modeIcon"
            class="mail-message-mode-btn"
            :title="modeTitle"
            :aria-label="modeTitle"
            @click="cycleMode"
          />
        </v-card-title>
        <v-card-subtitle>
          <span>{{ metaParts(item) }}</span>
          <span v-if="showInterestChips && interestProfiles(item).length" class="mail-interest-chip-row">
            <v-chip
              v-for="profile in interestProfiles(item)"
              :key="profile.id || profile.name"
              size="x-small"
              variant="tonal"
              color="primary"
              class="mail-interest-chip"
            >
              {{ profile.name }}
            </v-chip>
          </span>
        </v-card-subtitle>
        <v-card-text class="mail-card-body">
          <section v-if="item.model_name === 'llm/error'" class="mail-preview-section">
            <div class="mail-preview-label">Summary Status</div>
            <div class="mail-preview-text">{{ item.rationale || 'LLM summary unavailable.' }}</div>
          </section>
          <section v-if="item.has_summary && item.summary_text" class="mail-preview-section">
            <div class="mail-preview-label">Summary</div>
            <div class="mail-preview-text">{{ item.summary_text }}</div>
          </section>
          <section v-if="(isFull || showPreview) && hasPreview(item)" class="mail-preview-section">
            <div class="mail-preview-label">Preview</div>
            <div class="mail-preview-text">{{ item.preview_text }}</div>
          </section>
          <section v-if="isFull && !item.summary_text && !item.preview_text" class="mail-preview-section">
            <div class="mail-preview-label">Preview</div>
            <div class="mail-preview-text">No preview available yet.</div>
          </section>
          <section v-if="item.rationale && item.model_name !== 'llm/error'" class="mail-preview-section">
            <div class="mail-preview-label">Why It Scored</div>
            <div class="mail-preview-text">{{ item.rationale }}</div>
          </section>
        </v-card-text>
      </v-card>
    </article>
  `,
};

const MailMessageList = {
  components: { MailMessageItem },
  props: {
    items: { type: Array, default: () => [] },
    emptyMessage: { type: String, default: "No matching emails yet." },
    displayMode: { type: String, default: "comfort" },
    scoreKey: { type: String, default: "" },
    scoreLabel: { type: String, default: "" },
    showReadState: { type: Boolean, default: true },
    showInterestChips: { type: Boolean, default: false },
  },
  template: `
    <div class="stack mail-message-list">
      <p v-if="!items.length" class="subtle">{{ emptyMessage }}</p>
      <MailMessageItem
        v-for="item in items"
        :key="item.id"
        :item="item"
        :display-mode="displayMode"
        :score-key="scoreKey"
        :score-label="scoreLabel"
        :show-read-state="showReadState"
        :show-interest-chips="showInterestChips"
      />
    </div>
  `,
};

const SummaryEntryItem = {
  components: { MailMessageList },
  props: {
    entry: { type: Object, required: true },
    entryKey: { type: String, required: true },
    displayMode: { type: String, default: "compact" },
  },
  data() {
    return {
      expanded: false,
      loading: false,
      error: "",
      messages: [],
    };
  },
  computed: {
    messageIds() {
      return Array.isArray(this.entry?.message_ids)
        ? this.entry.message_ids.map((value) => Number(value)).filter((value) => Number.isFinite(value) && value > 0)
        : [];
    },
    messageCount() {
      return this.messageIds.length;
    },
    buttonLabel() {
      if (this.expanded) return "Hide evidence";
      return this.messageCount ? `Open ${this.messageCount} message${this.messageCount === 1 ? "" : "s"}` : "No messages";
    },
  },
  methods: {
    async toggleMessages() {
      if (!this.messageCount) return;
      this.expanded = !this.expanded;
      if (!this.expanded || this.messages.length || this.loading) return;
      this.loading = true;
      this.error = "";
      try {
        const responses = await Promise.all(
          this.messageIds.map((id) => fetchJson(`/api/messages/${id}`).then((data) => data.message))
        );
        this.messages = responses.map((message) => ({
          ...message,
          has_summary: Boolean(message.summary_text),
        }));
      } catch (error) {
        this.error = error.message;
      } finally {
        this.loading = false;
      }
    },
  },
  template: `
    <div class="summary-entry">
      <div class="summary-entry-main">
        <p>{{ entry.summary }}</p>
        <span>{{ messageCount }} message{{ messageCount === 1 ? '' : 's' }}</span>
      </div>
      <v-btn
        size="x-small"
        variant="text"
        class="summary-entry-open"
        :disabled="!messageCount"
        :loading="loading"
        :prepend-icon="expanded ? 'mdi-chevron-up' : 'mdi-email-open-outline'"
        @click="toggleMessages"
      >
        {{ buttonLabel }}
      </v-btn>
      <div v-if="expanded" class="summary-entry-messages">
        <p v-if="error" class="subtle">{{ error }}</p>
        <MailMessageList
          v-else
          :items="messages"
          empty-message="Loading messages..."
          :display-mode="displayMode"
          :show-read-state="false"
        />
      </div>
    </div>
  `,
};

const TriageView = {
  components: { MailMessageList, SummaryEntryItem },
  props: {
    messageDisplayMode: { type: String, default: "comfort" },
  },
  data() {
    const today = new Date();
    const monthStart = startOfMonth(today);
    return {
      loading: false,
      summaryLoading: false,
      summaryRebuildLoading: false,
      summaryAvailabilityLoading: false,
      error: "",
      summaryError: "",
      tab: "priority",
      triage: [],
      stars: messageStars.state,
      starred: [],
      starredMore: false,
      starredLoading: false,
      starredRequest: 0,
      profiles: [],
      categories: [],
      categoriesLoaded: false,
      topicSizeCap: null,
      categoriesRequestId: 0,
      categoriesPollTimer: null,
      categoriesAbortController: null,
      topicRenameEditing: false,
      topicRenameDraft: "",
      topicRenameBusy: false,
      topicRelabelBusy: false,
      topicActionMessage: "",
      moreCategories: false,
      categoriesLoading: false,
      categoriesLoadingMore: false,
      selectedCategoryId: null,
      summaries: [],
      calendarCursor: monthStart,
      monthPickerYear: today.getFullYear(),
      summarySelections: {
        day: isoDate(today),
        week: isoDate(startOfIsoWeek(today)),
        month: isoDate(monthStart),
      },
      summaryAvailability: {
        day: {},
        week: {},
        month: {},
      },
      dateRangePreset: "month",
      dateFrom: isoDate(monthStart),
      dateTo: isoDate(today),
      closedProfileMatches: {},
    };
  },
  computed: {
    visibleStarred() { return this.starred.filter((item) => !this.stars.ready || this.stars.values[item.id] > 0); },
    activeSummaryDuration() {
      return {
        "summary-day": "day",
        "summary-week": "week",
        "summary-month": "month",
      }[this.tab] || "";
    },
    activeSummaryTitle() {
      return {
        day: "Daily summary",
        week: "Weekly summary",
        month: "Monthly summary",
      }[this.activeSummaryDuration] || "Summary";
    },
    selectedSummaryLabel() {
      const duration = this.activeSummaryDuration;
      const startDate = this.summarySelections[duration];
      if (duration === "day") return formatDateOnly(startDate);
      if (duration === "week") {
        const start = parseIsoDate(startDate);
        const end = addDays(start, 6);
        return `${formatDateOnly(start)} to ${formatDateOnly(end)}`;
      }
      if (duration === "month") {
        return parseIsoDate(startDate).toLocaleDateString([], {
          month: "long",
          year: "numeric",
        });
      }
      return "";
    },
    summaryEmptyMessage() {
      return {
        day: "No daily summary for this date yet.",
        week: "No weekly summary for this week yet.",
        month: "No monthly summary for this month yet.",
      }[this.activeSummaryDuration] || "No summary for this interval yet.";
    },
    summaryCalendarTitle() {
      return this.calendarCursor.toLocaleDateString([], {
        month: "long",
        year: "numeric",
      });
    },
    summaryCalendarDays() {
      const first = startOfMonth(this.calendarCursor);
      const gridStart = startOfIsoWeek(first);
      const todayIso = isoDate(new Date());
      const selectedIso = this.summarySelections.day;
      return Array.from({ length: 42 }, (_, index) => {
        const date = addDays(gridStart, index);
        const dateIso = isoDate(date);
        return {
          iso: dateIso,
          label: date.getDate(),
          outside: date.getMonth() !== this.calendarCursor.getMonth(),
          selected: dateIso === selectedIso,
          today: dateIso === todayIso,
          hasData: Boolean(this.summaryAvailability.day[dateIso]),
        };
      });
    },
    summaryWeekRows() {
      const first = startOfMonth(this.calendarCursor);
      const gridStart = startOfIsoWeek(first);
      const selectedIso = this.summarySelections.week;
      return Array.from({ length: 6 }, (_, index) => {
        const start = addDays(gridStart, index * 7);
        const end = addDays(start, 6);
        const startIso = isoDate(start);
        return {
          iso: startIso,
          label: `${formatDateOnly(start)} - ${formatDateOnly(end)}`,
          selected: startIso === selectedIso,
          outside: start.getMonth() !== this.calendarCursor.getMonth() && end.getMonth() !== this.calendarCursor.getMonth(),
          hasData: Boolean(this.summaryAvailability.week[startIso]),
        };
      });
    },
    monthOptions() {
      return Array.from({ length: 12 }, (_, index) => {
        const date = new Date(this.monthPickerYear, index, 1);
        const dateIso = isoDate(date);
        return {
          iso: dateIso,
          label: date.toLocaleDateString([], { month: "short" }),
          selected: dateIso === this.summarySelections.month,
          hasData: Boolean(this.summaryAvailability.month[dateIso]),
        };
      });
    },
    visibleCategories() {
      return this.categories.filter((category) => Number(category.message_count) > 0);
    },
    selectedCategory() {
      return this.categories.find((category) => category.id === this.selectedCategoryId) || null;
    },
    categoryTags() {
      const cap = this.topicSizeCap || this.topicSizeReference(this.visibleCategories);
      return this.visibleCategories.map((category) => {
        const label = category.name || "Unnamed topic";
        const count = Number(category.message_count) || 0;
        const weight = Math.max(0, Math.min(1, Math.log(Math.max(1, count)) / Math.log(cap)));
        return { id: category.id, label, count, weight };
      });
    },
    dateRangeLabel() {
      if (this.dateRangePreset === "all") return "All time";
      if (this.dateFrom && this.dateTo) return `${formatDateOnly(this.dateFrom)} to ${formatDateOnly(this.dateTo)}`;
      if (this.dateFrom) return `From ${formatDateOnly(this.dateFrom)}`;
      if (this.dateTo) return `Through ${formatDateOnly(this.dateTo)}`;
      return "All time";
    },
  },
  watch: {
    "stars.revision"() { if (this.tab === "starred") this.loadStarred(); },
    tab(value) {
      if (value === "clusters") {
        this.loadCategories();
        return;
      }
      clearTimeout(this.categoriesPollTimer);
      this.categoriesPollTimer = null;
      this.categoriesAbortController?.abort();
      this.categoriesAbortController = null;
      this.categoriesRequestId += 1;
      this.categoriesLoading = false;
      this.categoriesLoadingMore = false;
      if (value === "starred") this.loadStarred();
      if (!this.isSummaryTab(value)) return;
      this.refreshCurrentTab();
    },
  },
  mounted() {
    this.load();
  },
  beforeUnmount() {
    clearTimeout(this.categoriesPollTimer);
    this.categoriesAbortController?.abort();
  },
  methods: {
    topicSizeReference(rows) {
      const counts = rows.map((row) => Number(row.message_count) || 1).sort((a, b) => a - b);
      if (!counts.length) return 2;
      const position = (counts.length - 1) * 0.95;
      const lower = Math.floor(position);
      const percentile = counts[lower] + (counts[Math.ceil(position)] - counts[lower]) * (position - lower);
      return Math.max(2, percentile);
    },
    isSummaryTab(value) {
      return value === "summary-day" || value === "summary-week" || value === "summary-month";
    },
    async loadStarred(more = false) {
      const request = ++this.starredRequest;
      this.starredLoading = true;
      this.error = "";
      try {
        await messageStars.load();
        const offset = more ? this.starred.length : 0;
        const data = await fetchJson(`/api/starred?limit=50&offset=${offset}`);
        if (request !== this.starredRequest) return;
        this.starred = more ? [...this.starred, ...data.messages] : data.messages;
        this.starredMore = data.has_more;
      } catch {
        if (request === this.starredRequest) this.error = "Could not load starred emails. Please try again.";
      } finally { if (request === this.starredRequest) this.starredLoading = false; }
    },
    async refreshCurrentTab() {
      if (this.tab === "starred") return this.loadStarred();
      if (this.isSummaryTab(this.tab)) {
        if (this.summaryLoading) return;
        await Promise.all([this.loadSummary(), this.loadSummaryAvailability()]);
      } else if (this.tab === "clusters") {
        await this.loadCategories();
      } else {
        await this.load();
      }
    },
    async load() {
      this.loading = true;
      this.error = "";
      try {
        const params = new URLSearchParams();
        if (this.dateFrom) params.set("from_date", this.dateFrom);
        if (this.dateTo) params.set("to_date", this.dateTo);
        const query = params.toString();
        const data = await fetchJson(`/api/triage${query ? "?" + query : ""}`);
        this.triage = data.triage || [];
        this.profiles = data.interest_profiles || [];
      } catch (error) {
        this.error = error.message;
      } finally {
        this.loading = false;
      }
    },
    setDateRangePreset(preset) {
      if (!preset) return;
      this.dateRangePreset = preset;
      const today = new Date();
      if (preset === "1d") {
        this.dateFrom = isoDate(today);
        this.dateTo = isoDate(today);
      } else if (preset === "7d") {
        this.dateFrom = isoDate(addDays(today, -6));
        this.dateTo = isoDate(today);
      } else if (preset === "month") {
        this.dateFrom = isoDate(startOfMonth(today));
        this.dateTo = isoDate(today);
      } else if (preset === "all") {
        this.dateFrom = "";
        this.dateTo = "";
      }
      this.reloadDateRange();
    },
    applyCustomDateRange() {
      this.dateRangePreset = "custom";
      if (this.dateFrom && this.dateTo && this.dateTo < this.dateFrom) {
        this.dateTo = this.dateFrom;
      }
      this.reloadDateRange();
    },
    async reloadDateRange() {
      clearTimeout(this.categoriesPollTimer);
      this.categoriesPollTimer = null;
      this.categoriesAbortController?.abort();
      this.categoriesAbortController = null;
      this.categoriesRequestId += 1;
      this.categoriesLoading = false;
      this.categoriesLoadingMore = false;
      this.categories = [];
      this.topicSizeCap = null;
      this.selectedCategoryId = null;
      await Promise.all(this.tab === "clusters" ? [this.load(), this.loadCategories()] : [this.load()]);
      if (this.tab !== "clusters") {
        this.categoriesLoaded = false;
        this.categories = [];
        this.selectedCategoryId = null;
      }
    },
    categoryListRow(category) {
      return { ...category, matched_messages: [], detailLoaded: false, detailLoading: false,
        messagesLoaded: false, messagesLoading: false, detailStale: false };
    },
    async loadCategories() {
      if (this.categoriesLoading) return;
      clearTimeout(this.categoriesPollTimer);
      this.categoriesPollTimer = null;
      const requestId = ++this.categoriesRequestId;
      const controller = new AbortController();
      this.categoriesAbortController = controller;
      this.categoriesLoading = true;
      if (!this.categoriesLoaded) this.error = "";
      let listLoaded = false;
      try {
        const requestedLimit = Math.max(200, this.categories.length);
        const params = new URLSearchParams({ limit: String(requestedLimit) });
        if (this.dateFrom) params.set("from_date", this.dateFrom);
        if (this.dateTo) params.set("to_date", this.dateTo);
        const data = await fetchJson(`/api/topics?${params}`, { signal: controller.signal });
        if (requestId !== this.categoriesRequestId) return;
        const rows = data.topics || [];
        listLoaded = true;
        if (this.topicSizeCap === null && rows.length) {
          this.topicSizeCap = this.topicSizeReference(rows.filter((row) => Number(row.message_count) > 0));
        }
        const old = new Map(this.categories.map((item) => [item.id, item]));
        const selectedBefore = old.get(this.selectedCategoryId);
        const selectedAfter = rows.find((row) => row.id === this.selectedCategoryId);
        const selectedChanged = selectedBefore && selectedAfter && (
          selectedBefore.updated_at !== selectedAfter.updated_at ||
          selectedBefore.message_count !== selectedAfter.message_count ||
          selectedBefore.name !== selectedAfter.name
        );
        const next = rows.map((row) => {
          const existing = old.get(row.id);
          if (!existing) return this.categoryListRow(row);
          if (existing.id === this.selectedCategoryId && selectedChanged) existing.detailStale = true;
          if (existing.name !== row.name || existing.message_count !== row.message_count ||
              existing.updated_at !== row.updated_at) Object.assign(existing, row);
          return existing;
        });
        if (next.length !== this.categories.length ||
            next.some((row, index) => row.id !== this.categories[index].id)) this.categories = next;
        this.moreCategories = rows.length === requestedLimit;
        this.categoriesLoaded = true;
        const selected = this.selectedCategory;
        if (!selected) {
          this.selectedCategoryId = null;
        } else if (selected.detailStale && selected.detailLoaded && selected.messagesLoaded &&
                   !selected.detailLoading && !selected.messagesLoading) {
          await this.refreshSelectedTopic(selected, controller.signal, requestId);
        }
      } catch (error) {
        if (requestId === this.categoriesRequestId && error.name !== "AbortError") this.error = error.message;
      } finally {
        if (requestId === this.categoriesRequestId) {
          this.categoriesLoading = false;
          this.categoriesAbortController = null;
          if (listLoaded && this.tab === "clusters") {
            this.categoriesPollTimer = setTimeout(() => this.loadCategories(), 12000);
          }
        }
      }
    },
    async refreshSelectedTopic(category, signal, requestId) {
      const params = new URLSearchParams();
      if (this.dateFrom) params.set("from_date", this.dateFrom);
      if (this.dateTo) params.set("to_date", this.dateTo);
      const targetCount = Math.max(50, category.matched_messages.length);
      const detail = await fetchJson(`/api/topics/${category.id}`, { signal });
      const messages = [];
      while (messages.length < targetCount) {
        const limit = Math.min(100, targetCount - messages.length);
        params.set("limit", String(limit));
        params.set("offset", String(messages.length));
        const page = await fetchJson(`/api/topics/${category.id}/messages?${params}`, { signal });
        messages.push(...(page.messages || []));
        if ((page.messages || []).length < limit) break;
      }
      if (requestId !== this.categoriesRequestId || this.selectedCategoryId !== category.id) return;
      Object.assign(category, detail.topic || {});
      category.matched_messages = messages;
      category.detailStale = false;
    },
    startTopicRename() {
      if (!this.selectedCategory) return;
      this.topicRenameDraft = this.selectedCategory.name || "";
      this.topicRenameEditing = true;
      this.topicActionMessage = "";
    },
    async saveTopicRename() {
      const name = this.topicRenameDraft.trim();
      if (!name || !this.selectedCategory || this.topicRenameBusy) return;
      this.topicRenameBusy = true;
      try {
        const data = await fetchJson(`/api/topics/${this.selectedCategory.id}`, {
          method: "PATCH", body: JSON.stringify({ name }),
        });
        Object.assign(this.selectedCategory, data.topic || { name });
        this.topicRenameEditing = false;
        this.topicActionMessage = "Topic renamed.";
      } catch (error) {
        this.error = error.message;
      } finally {
        this.topicRenameBusy = false;
      }
    },
    async requestTopicRelabel() {
      if (!this.selectedCategory || this.topicRelabelBusy) return;
      this.topicRelabelBusy = true;
      this.topicActionMessage = "";
      try {
        await fetchJson(`/api/topics/${this.selectedCategory.id}/relabel`, { method: "POST" });
        this.topicActionMessage = "AI rename queued.";
      } catch (error) {
        this.error = error.message;
      } finally {
        this.topicRelabelBusy = false;
      }
    },
    async loadSummary() {
      const duration = this.activeSummaryDuration || "day";
      const startDate = this.summarySelections[duration];
      this.summaryLoading = true;
      this.summaryError = "";
      this.summaries = [];
      try {
        const data = await fetchJson("/api/summaries", {
          method: "POST",
          body: JSON.stringify({ requests: [{ start_date: startDate, duration }] }),
        });
        this.summaries = data.summaries || [];
        this.markSummaryAvailability(duration, startDate, this.summaries);
      } catch (error) {
        this.summaryError = error.message;
      } finally {
        this.summaryLoading = false;
      }
    },
    async requestSummaryRebuild() {
      const duration = this.activeSummaryDuration || "day";
      const startDate = this.summarySelections[duration];
      this.summaryRebuildLoading = true;
      this.summaryError = "";
      try {
        await fetchJson("/api/summaries/request-rebuild", {
          method: "POST",
          body: JSON.stringify({ start_date: startDate, duration }),
        });
        await this.refreshCurrentTab();
      } catch (error) {
        this.summaryError = error.message;
      } finally {
        this.summaryRebuildLoading = false;
      }
    },
    async loadSummaryAvailability() {
      const duration = this.activeSummaryDuration || "day";
      const requests = this.buildSummaryAvailabilityRequests(duration);
      if (!requests.length) return;
      this.summaryAvailabilityLoading = true;
      try {
        const data = await fetchJson("/api/summaries", {
          method: "POST",
          body: JSON.stringify({ requests }),
        });
        const nextAvailability = {};
        for (const request of requests) {
          nextAvailability[request.start_date] = false;
        }
        for (const item of data.summaries || []) {
          const key = item.interval_start_date;
          if (key in nextAvailability && this.summaryRowHasData(item)) {
            nextAvailability[key] = true;
          }
        }
        this.summaryAvailability = {
          ...this.summaryAvailability,
          [duration]: nextAvailability,
        };
      } catch {
        // Availability is only a visual hint; keep the selected summary error path separate.
      } finally {
        this.summaryAvailabilityLoading = false;
      }
    },
    buildSummaryAvailabilityRequests(duration) {
      if (duration === "day") {
        return this.summaryCalendarDays.map((day) => ({ start_date: day.iso, duration: "day" }));
      }
      if (duration === "week") {
        return this.summaryWeekRows.map((week) => ({ start_date: week.iso, duration: "week" }));
      }
      if (duration === "month") {
        return this.monthOptions.map((month) => ({ start_date: month.iso, duration: "month" }));
      }
      return [];
    },
    summaryRowHasData(item) {
      return this.summaryEntries(item).length > 0 || Boolean(item.refreshed_at);
    },
    summaryEntries(item) {
      const payload = item.summary_json || {};
      const entries = Array.isArray(payload.entries) ? payload.entries : [];
      return entries.filter((entry) => String(entry?.summary || "").trim());
    },
    summaryEntryMessageCount(entry) {
      const ids = Array.isArray(entry?.message_ids) ? entry.message_ids : [];
      return ids.length;
    },
    markSummaryAvailability(duration, startDate, rows) {
      this.summaryAvailability = {
        ...this.summaryAvailability,
        [duration]: {
          ...this.summaryAvailability[duration],
          [startDate]: (rows || []).some((item) => this.summaryRowHasData(item)),
        },
      };
    },
    selectSummaryInterval(duration, startDate) {
      this.summarySelections = { ...this.summarySelections, [duration]: startDate };
      if (duration === "day") {
        this.calendarCursor = startOfMonth(parseIsoDate(startDate));
      }
      if (duration === "month") {
        this.monthPickerYear = parseIsoDate(startDate).getFullYear();
      }
      this.loadSummary();
    },
    shiftSummaryCalendar(months) {
      this.calendarCursor = startOfMonth(addMonths(this.calendarCursor, months));
      if (this.activeSummaryDuration === "day" || this.activeSummaryDuration === "week") {
        this.loadSummaryAvailability();
      }
    },
    shiftSummaryYear(years) {
      this.monthPickerYear += years;
      if (this.activeSummaryDuration === "month") {
        this.loadSummaryAvailability();
      }
    },
    formatSummaryTitle(item) {
      const account = item.account_name || "";
      const mailbox = item.mailbox_name || item.provider_mailbox_id || `Mailbox ${item.mailbox_id}`;
      return account ? `${account} / ${mailbox}` : mailbox;
    },
    formatSummaryMeta(item) {
      const refreshed = formatDate(item.refreshed_at);
      const freshness = refreshed ? `refreshed ${refreshed}` : "pending";
      return `${item.summary_type || "summary"} | ${Number(item.period_days || 0)} day${Number(item.period_days || 0) === 1 ? "" : "s"} | ${freshness}`;
    },
    visibleProfileMessages(profile) {
      return profile.matched_messages || [];
    },
    compactProfileSummary(item) {
      return item.summary_text || item.preview_text || item.profile_match_rationale || "No summary yet.";
    },
    visibleCategoryMessages(category) {
      return category.matched_messages || [];
    },
    isProfileClosed(profile) {
      return Boolean(this.closedProfileMatches[profile.id]);
    },
    toggleProfileMatches(profile) {
      this.closedProfileMatches = { ...this.closedProfileMatches, [profile.id]: !this.isProfileClosed(profile) };
    },
    async loadMoreCategories() {
      if (this.categoriesLoadingMore) return;
      if (this.categoriesLoading) {
        this.categoriesAbortController?.abort();
        this.categoriesAbortController = null;
        this.categoriesRequestId += 1;
        this.categoriesLoading = false;
      }
      clearTimeout(this.categoriesPollTimer);
      this.categoriesPollTimer = null;
      const requestId = ++this.categoriesRequestId;
      const controller = new AbortController();
      this.categoriesAbortController = controller;
      this.categoriesLoading = true;
      this.categoriesLoadingMore = true;
      let loaded = false;
      try {
        const params = new URLSearchParams({ limit: "200", offset: String(this.categories.length) });
        if (this.dateFrom) params.set("from_date", this.dateFrom);
        if (this.dateTo) params.set("to_date", this.dateTo);
        const data = await fetchJson(`/api/topics?${params}`, { signal: controller.signal });
        if (requestId !== this.categoriesRequestId) return;
        const rows = data.topics || [];
        this.categories.push(...rows.map(this.categoryListRow));
        this.topicSizeCap = this.topicSizeReference(this.visibleCategories);
        this.moreCategories = rows.length === 200;
        loaded = true;
      } catch (error) {
        if (requestId === this.categoriesRequestId && error.name !== "AbortError") this.error = error.message;
      } finally {
        if (requestId === this.categoriesRequestId) {
          this.categoriesLoading = false;
          this.categoriesLoadingMore = false;
          this.categoriesAbortController = null;
          if (loaded && this.tab === "clusters") {
            this.categoriesPollTimer = setTimeout(() => this.loadCategories(), 12000);
          }
        }
      }
    },
    async loadCategoryMessages(category, signal) {
      if (category.messagesLoading) return;
      category.messagesLoading = true;
      try {
        const params = new URLSearchParams({ limit: "50", offset: String(category.matched_messages.length) });
        if (this.dateFrom) params.set("from_date", this.dateFrom);
        if (this.dateTo) params.set("to_date", this.dateTo);
        const data = await fetchJson(`/api/topics/${category.id}/messages?${params}`, { signal });
        category.matched_messages.push(...(data.messages || []));
        category.messagesLoaded = true;
      } catch (error) {
        if (error.name !== "AbortError") this.error = error.message;
      } finally {
        category.messagesLoading = false;
      }
    },
    async loadCategoryDetail(category, signal) {
      if (category.detailLoading || category.detailLoaded) return;
      category.detailLoading = true;
      try {
        const data = await fetchJson(`/api/topics/${category.id}`, { signal });
        Object.assign(category, data.topic || {});
        category.detailLoaded = true;
      } catch (error) {
        if (error.name !== "AbortError") this.error = error.message;
      } finally {
        category.detailLoading = false;
      }
    },
    async focusCategoryFromTag(categoryId) {
      if (this.selectedCategoryId === categoryId) {
        this.selectedCategoryId = null;
        return;
      }
      this.selectedCategoryId = categoryId;
      this.topicRenameEditing = false;
      this.topicActionMessage = "";
      const category = this.categories.find(item => item.id === categoryId);
      if (category) await Promise.all([
        this.loadCategoryDetail(category),
        category.messagesLoaded ? Promise.resolve() : this.loadCategoryMessages(category),
      ]);
      await this.$nextTick();
      const element = document.getElementById(`topic-category-${categoryId}`);
      if (!element) return;
      element.scrollIntoView({ behavior: "smooth", block: "start" });
    },
  },
  template: `
    <v-card class="view-card" variant="flat">
      <div class="section-header">
        <div>
          <h2>Triage</h2>
        </div>
        <div class="inline-actions">
          <div v-if="tab !== 'starred'" class="triage-date-controls">
            <v-btn-toggle class="triage-range-toggle" v-model="dateRangePreset" density="compact" variant="tonal" divided @update:model-value="setDateRangePreset">
              <v-btn size="small" value="1d">1d</v-btn>
              <v-btn size="small" value="7d">7d</v-btn>
              <v-btn size="small" value="month">Month</v-btn>
              <v-btn size="small" value="all">All</v-btn>
            </v-btn-toggle>
            <label class="triage-date-field">
              <span>From</span>
              <input v-model="dateFrom" type="date" :max="dateTo || undefined" @change="applyCustomDateRange" />
            </label>
            <label class="triage-date-field">
              <span>To</span>
              <input v-model="dateTo" type="date" :min="dateFrom || undefined" @change="applyCustomDateRange" />
            </label>
            <span class="triage-date-label">{{ dateRangeLabel }}</span>
          </div>
          <v-btn color="primary" :loading="isSummaryTab(tab) ? summaryLoading : tab === 'clusters' ? categoriesLoading && !categoriesLoaded : loading" prepend-icon="mdi-refresh" @click="refreshCurrentTab">Refresh</v-btn>
        </div>
      </div>
      <v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-3">{{ error }}</v-alert>
      <v-alert v-if="summaryError && isSummaryTab(tab)" type="error" variant="tonal" density="compact" class="mb-3">{{ summaryError }}</v-alert>
      <v-tabs v-model="tab" density="compact" class="triage-tabs mb-3">
        <v-tab value="priority" prepend-icon="mdi-priority-high">
          <span class="triage-tab-label">
            <span>Priority Triage</span>
            <v-chip size="x-small" variant="tonal" color="primary" class="count-chip">{{ triage.length }}</v-chip>
          </span>
        </v-tab>
        <v-tab value="starred" prepend-icon="mdi-star">Starred</v-tab>
        <v-tab value="profiles" prepend-icon="mdi-account-heart-outline">Interest Profiles</v-tab>
        <v-tab value="clusters" prepend-icon="mdi-graph-outline">Topics</v-tab>
        <v-tab value="summary-day" prepend-icon="mdi-calendar-today">Daily</v-tab>
        <v-tab value="summary-week" prepend-icon="mdi-calendar-week">Weekly</v-tab>
        <v-tab value="summary-month" prepend-icon="mdi-calendar-month">Monthly</v-tab>
      </v-tabs>
      <v-window v-model="tab">
        <v-window-item value="priority">
          <MailMessageList :items="triage" empty-message="No triaged mail yet." :display-mode="messageDisplayMode" :show-read-state="false" :show-interest-chips="true" />
        </v-window-item>
        <v-window-item value="starred">
          <p class="subtle">Starred emails · All dates · Newest first</p>
          <v-progress-linear v-if="starredLoading" indeterminate />
          <MailMessageList :items="visibleStarred" :empty-message="starredLoading ? 'Loading starred emails…' : 'No starred emails yet.'" :display-mode="messageDisplayMode" />
          <v-btn v-if="starredMore" :loading="starredLoading" @click="loadStarred(true)">Load more</v-btn>
        </v-window-item>
        <v-window-item value="profiles">
          <div class="stack">
            <p v-if="!profiles.length" class="subtle">No interest profiles yet.</p>
            <v-card v-for="profile in profiles" :key="profile.id" class="group-card" variant="flat">
              <v-card-title>{{ profile.name || 'Unnamed profile' }}</v-card-title>
              <v-card-subtitle>
                {{ visibleProfileMessages(profile).length }} of {{ (profile.matched_messages || []).length }} matching emails | weight {{ profile.priority_weight }} | prominence {{ profile.dashboard_prominence }} | {{ profile.summary_detail }} detail | {{ profile.watch_mode }}
              </v-card-subtitle>
              <v-card-text>
                <p>{{ profile.description || 'No description.' }}</p>
                <p class="subtle">Signals: {{ profile.signals || 'none' }}</p>
                <p class="subtle">Collaborators: {{ (profile.collaborators_json || []).join(', ') || 'none' }}</p>
                <div class="section-header compact-section-header">
                  <p class="section-label">Matching emails: {{ visibleProfileMessages(profile).length }} / {{ (profile.matched_messages || []).length }}</p>
                </div>
                <MailMessageList :items="visibleProfileMessages(profile)" empty-message="No emails currently match this interest profile." :display-mode="messageDisplayMode" score-key="profile_match_score" score-label="profile match" />
              </v-card-text>
            </v-card>
          </div>
        </v-window-item>
        <v-window-item value="clusters">
          <div class="stack">
            <div v-if="categoryTags.length" class="category-cloud">
              <button
                v-for="tag in categoryTags"
                :key="tag.id"
                type="button"
                class="category-cloud-tag"
                :class="{ 'is-selected': selectedCategoryId === tag.id }"
                :aria-pressed="selectedCategoryId === tag.id"
                :style="{ '--tag-weight': tag.weight }"
                @click="focusCategoryFromTag(tag.id)"
              >
                <span>{{ tag.label }}</span>
                <span class="category-cloud-count">{{ tag.count }}</span>
              </button>
            </div>
            <v-progress-linear v-if="categoriesLoading && !categoriesLoaded" indeterminate />
            <p v-if="categoriesLoaded && !visibleCategories.length" class="subtle">No topics with matching emails right now.</p>
            <v-card v-if="selectedCategory" :id="'topic-category-' + selectedCategory.id" :key="selectedCategory.id" class="group-card" variant="flat">
              <v-card-title class="d-flex align-center ga-2">
                <span>{{ selectedCategory.name || 'Unnamed topic' }}</span>
                <v-btn icon="mdi-pencil" size="x-small" variant="text" aria-label="Rename topic" @click="startTopicRename" />
                <v-btn icon="mdi-auto-fix" size="x-small" variant="text" aria-label="Rename topic with AI" :loading="topicRelabelBusy" @click="requestTopicRelabel" />
              </v-card-title>
              <v-card-subtitle>{{ visibleCategoryMessages(selectedCategory).length }} of {{ selectedCategory.message_count || 0 }} clustered emails</v-card-subtitle>
              <v-card-text>
                <div v-if="topicRenameEditing" class="inline-actions">
                  <v-text-field v-model="topicRenameDraft" label="Topic name" density="compact" maxlength="160" @keyup.enter="saveTopicRename" />
                  <v-btn size="small" :loading="topicRenameBusy" @click="saveTopicRename">Save</v-btn>
                  <v-btn size="small" variant="text" @click="topicRenameEditing = false">Cancel</v-btn>
                </div>
                <p v-if="topicActionMessage" class="subtle">{{ topicActionMessage }}</p>
                <v-progress-linear v-if="selectedCategory.detailLoading" indeterminate class="mb-2" />
                <template v-if="selectedCategory.detailLoaded">
                  <p>{{ selectedCategory.description || 'No description.' }}</p>
                  <p class="subtle">Key terms: {{ (selectedCategory.key_terms_json || []).join(', ') || 'none' }}</p>
                </template>
                <div class="section-header compact-section-header">
                  <p class="section-label">Topic emails: {{ visibleCategoryMessages(selectedCategory).length }} / {{ selectedCategory.message_count || 0 }}</p>
                </div>
                <MailMessageList v-if="selectedCategory.messagesLoaded" :items="visibleCategoryMessages(selectedCategory)" empty-message="No emails are attached to this topic yet." :display-mode="messageDisplayMode" score-key="topic_match_score" score-label="topic match" />
                <v-progress-linear v-if="selectedCategory.messagesLoading" indeterminate class="mt-2" />
                <v-btn v-if="selectedCategory.messagesLoaded && visibleCategoryMessages(selectedCategory).length < selectedCategory.message_count" size="small" variant="tonal" :loading="selectedCategory.messagesLoading" @click="loadCategoryMessages(selectedCategory)">Load more emails</v-btn>
              </v-card-text>
            </v-card>
            <v-btn v-if="moreCategories" variant="tonal" :loading="categoriesLoadingMore" @click="loadMoreCategories">Load more topics</v-btn>
          </div>
        </v-window-item>
        <v-window-item value="summary-day">
          <div class="summary-tab-layout">
            <section class="summary-picker" aria-label="Daily summary calendar">
              <div class="summary-picker-head">
                <v-btn icon="mdi-chevron-left" variant="tonal" size="small" aria-label="Previous month" @click="shiftSummaryCalendar(-1)" />
                <strong>{{ summaryCalendarTitle }}</strong>
                <v-btn icon="mdi-chevron-right" variant="tonal" size="small" aria-label="Next month" @click="shiftSummaryCalendar(1)" />
              </div>
              <div class="summary-calendar-labels">
                <span>Mon</span><span>Tue</span><span>Wed</span><span>Thu</span><span>Fri</span><span>Sat</span><span>Sun</span>
              </div>
              <div class="summary-calendar-grid">
                <button
                  v-for="day in summaryCalendarDays"
                  :key="day.iso"
                  type="button"
                  class="summary-calendar-day"
                  :class="{ 'is-outside': day.outside, 'is-selected': day.selected, 'is-today': day.today, 'has-data': day.hasData, 'has-no-data': !day.hasData }"
                  @click="selectSummaryInterval('day', day.iso)"
                >
                  {{ day.label }}
                </button>
              </div>
            </section>
            <section class="summary-results">
              <div class="summary-tab-head">
                <div>
                  <h3>{{ activeSummaryTitle }}</h3>
                  <p class="subtle">{{ selectedSummaryLabel }}</p>
                </div>
                <v-btn size="small" variant="tonal" :loading="summaryRebuildLoading" prepend-icon="mdi-autorenew" @click="requestSummaryRebuild">Request rebuild</v-btn>
              </div>
              <div class="summary-grid">
                <p v-if="!summaryLoading && !summaries.length" class="subtle">{{ summaryEmptyMessage }}</p>
                <v-card v-for="item in summaries" :key="'summary-day-' + item.id" class="summary-card" variant="flat">
                  <v-card-title>{{ formatSummaryTitle(item) }}</v-card-title>
                  <v-card-subtitle>{{ formatSummaryMeta(item) }}</v-card-subtitle>
                  <v-card-text>
                    <p v-if="!summaryEntries(item).length" class="subtle">Summary has not been generated yet.</p>
                    <SummaryEntryItem
                      v-for="(entry, index) in summaryEntries(item)"
                      :key="'summary-day-entry-' + item.id + '-' + index"
                      :entry="entry"
                      :entry-key="'summary-day-entry-' + item.id + '-' + index"
                      :display-mode="messageDisplayMode"
                    />
                  </v-card-text>
                </v-card>
              </div>
            </section>
          </div>
        </v-window-item>
        <v-window-item value="summary-week">
          <div class="summary-tab-layout">
            <section class="summary-picker" aria-label="Weekly summary calendar">
              <div class="summary-picker-head">
                <v-btn icon="mdi-chevron-left" variant="tonal" size="small" aria-label="Previous month" @click="shiftSummaryCalendar(-1)" />
                <strong>{{ summaryCalendarTitle }}</strong>
                <v-btn icon="mdi-chevron-right" variant="tonal" size="small" aria-label="Next month" @click="shiftSummaryCalendar(1)" />
              </div>
              <div class="summary-week-list">
                <button
                  v-for="week in summaryWeekRows"
                  :key="week.iso"
                  type="button"
                  class="summary-week-row"
                  :class="{ 'is-outside': week.outside, 'is-selected': week.selected, 'has-data': week.hasData, 'has-no-data': !week.hasData }"
                  @click="selectSummaryInterval('week', week.iso)"
                >
                  <span>{{ week.label }}</span>
                  <span>Week</span>
                </button>
              </div>
            </section>
            <section class="summary-results">
              <div class="summary-tab-head">
                <div>
                  <h3>{{ activeSummaryTitle }}</h3>
                  <p class="subtle">{{ selectedSummaryLabel }}</p>
                </div>
                <v-btn size="small" variant="tonal" :loading="summaryRebuildLoading" prepend-icon="mdi-autorenew" @click="requestSummaryRebuild">Request rebuild</v-btn>
              </div>
              <div class="summary-grid">
                <p v-if="!summaryLoading && !summaries.length" class="subtle">{{ summaryEmptyMessage }}</p>
                <v-card v-for="item in summaries" :key="'summary-week-' + item.id" class="summary-card" variant="flat">
                  <v-card-title>{{ formatSummaryTitle(item) }}</v-card-title>
                  <v-card-subtitle>{{ formatSummaryMeta(item) }}</v-card-subtitle>
                  <v-card-text>
                    <p v-if="!summaryEntries(item).length" class="subtle">Summary has not been generated yet.</p>
                    <SummaryEntryItem
                      v-for="(entry, index) in summaryEntries(item)"
                      :key="'summary-week-entry-' + item.id + '-' + index"
                      :entry="entry"
                      :entry-key="'summary-week-entry-' + item.id + '-' + index"
                      :display-mode="messageDisplayMode"
                    />
                  </v-card-text>
                </v-card>
              </div>
            </section>
          </div>
        </v-window-item>
        <v-window-item value="summary-month">
          <div class="summary-tab-layout">
            <section class="summary-picker" aria-label="Monthly summary calendar">
              <div class="summary-picker-head">
                <v-btn icon="mdi-chevron-left" variant="tonal" size="small" aria-label="Previous year" @click="shiftSummaryYear(-1)" />
                <strong>{{ monthPickerYear }}</strong>
                <v-btn icon="mdi-chevron-right" variant="tonal" size="small" aria-label="Next year" @click="shiftSummaryYear(1)" />
              </div>
              <div class="summary-month-grid">
                <button
                  v-for="month in monthOptions"
                  :key="month.iso"
                  type="button"
                  class="summary-month-cell"
                  :class="{ 'is-selected': month.selected, 'has-data': month.hasData, 'has-no-data': !month.hasData }"
                  @click="selectSummaryInterval('month', month.iso)"
                >
                  {{ month.label }}
                </button>
              </div>
            </section>
            <section class="summary-results">
              <div class="summary-tab-head">
                <div>
                  <h3>{{ activeSummaryTitle }}</h3>
                  <p class="subtle">{{ selectedSummaryLabel }}</p>
                </div>
                <v-btn size="small" variant="tonal" :loading="summaryRebuildLoading" prepend-icon="mdi-autorenew" @click="requestSummaryRebuild">Request rebuild</v-btn>
              </div>
              <div class="summary-grid">
                <p v-if="!summaryLoading && !summaries.length" class="subtle">{{ summaryEmptyMessage }}</p>
                <v-card v-for="item in summaries" :key="'summary-month-' + item.id" class="summary-card" variant="flat">
                  <v-card-title>{{ formatSummaryTitle(item) }}</v-card-title>
                  <v-card-subtitle>{{ formatSummaryMeta(item) }}</v-card-subtitle>
                  <v-card-text>
                    <p v-if="!summaryEntries(item).length" class="subtle">Summary has not been generated yet.</p>
                    <SummaryEntryItem
                      v-for="(entry, index) in summaryEntries(item)"
                      :key="'summary-month-entry-' + item.id + '-' + index"
                      :entry="entry"
                      :entry-key="'summary-month-entry-' + item.id + '-' + index"
                      :display-mode="messageDisplayMode"
                    />
                  </v-card-text>
                </v-card>
              </div>
            </section>
          </div>
        </v-window-item>
      </v-window>
    </v-card>
  `,
};

const HomeView = {
  components: { TriageView },
  props: {
    messageDisplayMode: { type: String, default: "comfort" },
  },
  template: `
    <div class="home-stack">
      <TriageView :message-display-mode="messageDisplayMode" />
    </div>
  `,
};

const MailSettingsView = {
  components: { GoogleOAuthSetup },
  data() {
    return {
      form: defaultAccountForm(),
      accounts: [],
      jobs: [],
      accountMailboxes: {},
      status: "",
      loadingAccounts: false,
      loadingJobs: false,
      saving: false,
      jobPoller: null,
      accountFormOpen: false,
    };
  },
  computed: {
    fieldHelp() {
      return {
        name: "Friendly label shown in the UI. Use something like 'Work Email' or 'Personal IMAP'.",
        provider: "Select the connector type. IMAP is for standard IMAP servers. EWS is for Microsoft Exchange Web Services.",
        password: "Secret used for login. For password-based IMAP this is the password or app password. Gmail OAuth uses Connect Google instead.",
        secretLocator: "Environment variable name where the secret is stored. Use this when you want the app to read credentials from the environment instead of storing them directly.",
        authMode: "How credentials are resolved. 'env' means the app reads the secret from the environment variable named in Secret locator.",
        active: "Disabled accounts stay saved but are skipped by background sync and manual sync.",
        host: "IMAP server hostname.",
        port: "IMAP server port. 993 is the normal SSL IMAP port.",
        username: "Login identity for IMAP or EWS. Usually your email address or a domain username.",
        syncWindowDays: "Only unread messages received in the last N days are ingested. Use 30, 60, or 90 to limit backfill size. Leave blank for no date limit.",
        ssl: "Enable TLS/SSL for IMAP connections. Keep this on unless your mail server explicitly requires plain-text IMAP.",
        ignoreTlsErrors: "Skip TLS certificate validation. Enable this for self-signed or mismatched certificates on the IMAP server.",
        endpoint: "Full Exchange Web Services endpoint URL, usually ending in /EWS/Exchange.asmx.",
      };
    },

    showEws() {
      return this.form.provider_type === "ews";
    },
    showGoogle() {
      return this.form.provider_type === "gmail";
    },
    showImapFields() {
      return this.form.provider_type === "imap";
    },
    showUsername() {
      return this.showImapFields || this.showEws || this.showGoogle;
    },
    showAccountForm() {
      return this.accountFormOpen || !this.accounts.length;
    },
    accountFormTitle() {
      return this.form.id ? "Edit Account" : "Add Account";
    },
    accountSubmitLabel() {
      return this.form.id ? "Save changes" : "Add account";
    },
  },
  mounted() {
    const outcome = this.$route.query.google;
    const messages = {
      connected: 'Google connected. Choose mailboxes, then sync.',
      cancelled: 'Google connection cancelled. You can try again.',
      expired: 'Google connection expired. Click Connect Google again.',
      permission: 'Grant mail access in Google consent, then reconnect.',
      'wrong-account': 'Google email did not match this mailbox. Reconnect using the correct Google account.',
      'account-changed': 'The mailbox changed during authorization. Save it and reconnect.',
      failed: 'Google connection failed. Check Google setup and try again.',
    };
    if (outcome) {
      this.status = messages[outcome] || 'Google connection did not complete.';
      this.$router.replace('/mail-settings');
    }
    this.loadAccounts();
    this.loadJobs();
    this.jobPoller = setInterval(() => this.loadJobs(false), 3000);
  },
  unmounted() {
    if (this.jobPoller) clearInterval(this.jobPoller);
  },
  methods: {
    fetchJson,
    async connectGoogle(account) {
      try {
        const data = await fetchJson(`/api/accounts/${account.id}/google-oauth/start`, { method: "POST" });
        location.assign(data.authorization_url);
      } catch (error) { this.status = error.message; }
    },
    providerLabel(type) {
      if (type === "ews") return "Exchange / EWS";
      return "IMAP";
    },
    getStatusColor(status) {
      if (status === "running") return "primary";
      if (status === "completed") return "success";
      if (status === "failed") return "error";
      return "grey";
    },
    accountSummary(account) {
      const settings = account.settings_json || {};
      const syncWindow = settings.sync_window_days ? ` | last ${settings.sync_window_days} days` : " | all unread";
      if (account.provider_type === "ews") return `${settings.endpoint || "No endpoint configured."}${syncWindow}`;
      return `${settings.host || "unknown host"}:${settings.port || 993} as ${settings.username || "unknown user"}${syncWindow}`;
    },
    formFromAccount(account) {
      const settings = account.settings_json || {};
      const form = defaultAccountForm();
      form.id = account.id;
      form.name = account.name || "";
      form.provider_type = account.auth_mode === "google_oauth" ? "gmail" : (account.provider_type || "imap");
      form.password = "";
      form.secret_locator = account.secret_locator || "";
      form.auth_mode = account.auth_mode || "env";
      form.active = account.status !== "disabled";

      form.host = settings.host || "";
      form.port = settings.port || "";
      form.username = settings.username || "";
      form.sync_window_days = settings.sync_window_days || "";
      form.ssl = settings.ssl !== false;
      form.ignore_tls_errors = settings.ignore_tls_errors === true;
      form.endpoint = settings.endpoint || "";
      return form;
    },
    buildPayload() {
      const payload = {
        id: this.form.id,
        name: this.form.name.trim(),
        provider_type: this.showGoogle ? "imap" : this.form.provider_type,
        auth_mode: this.showGoogle ? "google_oauth" : (this.form.auth_mode === "google_oauth" ? "config" : this.form.auth_mode.trim() || "env"),
        status: this.form.active ? "active" : "disabled",
        secret_locator: this.showGoogle ? "" : this.form.secret_locator.trim(),
        password: this.showGoogle ? "" : this.form.password,
        settings: {},
      };
      if (this.form.sync_window_days) payload.sync_window_days = Number(this.form.sync_window_days);
      if (this.form.provider_type === "ews") {
        payload.settings = { endpoint: this.form.endpoint.trim(), username: this.form.username.trim() };
      } else {
        payload.settings = {
          host: this.form.host.trim() || "",
          port: this.form.port || "993",
          username: this.form.username.trim(),
          ssl: this.form.ssl,
          ignore_tls_errors: this.form.ignore_tls_errors,
        };
      }
      return payload;
    },
    resetForm() {
      this.form = defaultAccountForm();
    },
    newAccount() {
      this.resetForm();
      this.accountFormOpen = true;
      this.status = "";
    },
    closeAccountForm() {
      this.resetForm();
      this.accountFormOpen = false;
      this.status = "";
    },
    editAccount(account) {
      this.form = this.formFromAccount(account);
      this.accountFormOpen = true;
      this.status = account.auth_mode === "google_oauth" ? `Editing ${account.name}.` : `Editing ${account.name}. Leave password blank to keep the saved password.`;
    },
    async saveAccount() {
      if (!this.form.name.trim()) {
        this.status = "Display name is required.";
        return;
      }
      const editingId = this.form.id;
      this.saving = true;
      this.status = "Saving account...";
      try {
        const data = await fetchJson("/api/accounts", { method: "POST", body: JSON.stringify(this.buildPayload()) });
        this.status = "Account saved.";
        await this.loadAccounts();
        const saved = this.accounts.find((account) => Number(account.id) === Number(data.account_id));
        if (!this.accounts.length) {
          this.resetForm();
          this.accountFormOpen = true;
        } else if (editingId && saved) {
          this.form = this.formFromAccount(saved);
          this.accountFormOpen = false;
        } else {
          this.resetForm();
          this.accountFormOpen = false;
        }
      } catch (error) {
        this.status = error.message;
      } finally {
        this.saving = false;
      }
    },
    async loadAccounts() {
      this.loadingAccounts = true;
      try {
        const data = await fetchJson("/api/accounts");
        this.accounts = data.accounts || [];
        if (!this.accounts.length) this.accountFormOpen = true;
      } catch (error) {
        this.status = error.message;
      } finally {
        this.loadingAccounts = false;
      }
    },
    async loadJobs(showLoading = true) {
      if (showLoading) this.loadingJobs = true;
      try {
        const data = await fetchJson("/api/jobs");
        this.jobs = data.jobs || [];
      } catch (error) {
        this.status = error.message;
      } finally {
        if (showLoading) this.loadingJobs = false;
      }
    },
    async discoverMailboxes(account) {
      this.status = `Loading mailboxes for ${account.name}...`;
      try {
        const data = await fetchJson(`/api/accounts/${account.id}/discover-mailboxes`, { method: "POST" });
        this.accountMailboxes[account.id] = data.mailboxes || [];
        const monitored = (data.mailboxes || []).filter((mailbox) => mailbox.monitored).length;
        this.status = `${account.name}: ${monitored} of ${(data.mailboxes || []).length} mailboxes selected for sync.`;
      } catch (error) {
        this.status = error.message;
      }
    },
    async updateMailbox(mailbox) {
      try {
        await fetchJson(`/api/mailboxes/${mailbox.id}`, { method: "PATCH", body: JSON.stringify({ monitored: mailbox.monitored }) });
      } catch (error) {
        mailbox.monitored = !mailbox.monitored;
        this.status = error.message;
      }
    },
    async syncAccount(account) {
      this.status = `Syncing ${account.name}...`;
      try {
        await this.loadJobs(false);
        const result = await fetchJson(`/api/sync/${account.id}`, { method: "POST" });
        this.status = `${account.name}: scheduled ${result.scheduled_mailboxes || 0} mailbox${Number(result.scheduled_mailboxes || 0) === 1 ? "" : "es"} in job #${result.job_id || "unknown"}.`;
        await this.loadJobs();
        this.$router.push("/");
      } catch (error) {
        this.status = error.message;
      }
    },
    async deleteAccount(account) {
      if (!confirm(`Delete saved account "${account.name}"? This also removes its discovered mailboxes and synced messages.`)) return;
      try {
        await fetchJson(`/api/accounts/${account.id}`, { method: "DELETE" });
        this.status = `${account.name} deleted.`;
        if (Number(this.form.id) === Number(account.id)) this.resetForm();
        await this.loadAccounts();
        this.accountFormOpen = !this.accounts.length;
      } catch (error) {
        this.status = error.message;
      }
    },
  },
  template: `
    <SettingsPage title="Mail Accounts">
      <div class="settings-layout">
        <v-card v-if="showAccountForm" class="view-card" variant="flat">
          <div class="section-header">
            <div><h2>{{ accountFormTitle }}</h2><p class="subtle">Credentials are encrypted locally before storage.</p></div>
            <v-btn v-if="accounts.length" class="settings-close-button" icon="mdi-close" variant="text" aria-label="Close account form" title="Close account form" @click="closeAccountForm" />
          </div>
          <v-form class="settings-form-vuetify" @submit.prevent="saveAccount">
            <v-text-field v-model="form.name" label="Display name" placeholder="Work IMAP" required density="compact">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ fieldHelp.name }}</v-tooltip></template>
            </v-text-field>
            <v-select v-model="form.provider_type" label="Provider" :items="[{title:'IMAP',value:'imap'},{title:'Gmail / Google OAuth',value:'gmail'},{title:'Exchange / EWS',value:'ews'}]" density="compact">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ fieldHelp.provider }}</v-tooltip></template>
            </v-select>
            <div v-if="showGoogle" class="full-width">Sign in with Google after saving this account. <GoogleOAuthSetup :fetch-json="fetchJson" /></div>
            <v-text-field v-if="!showGoogle" v-model="form.password" label="Password" type="password" density="compact">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ fieldHelp.password }}</v-tooltip></template>
            </v-text-field>
            <v-text-field v-if="!showGoogle" v-model="form.secret_locator" label="Secret locator" placeholder="MAIL_IMAP_PASSWORD" density="compact">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ fieldHelp.secretLocator }}</v-tooltip></template>
            </v-text-field>
            <v-text-field v-if="!showGoogle" v-model="form.auth_mode" label="Auth mode" density="compact">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ fieldHelp.authMode }}</v-tooltip></template>
            </v-text-field>
            <div class="full-width">
              <v-switch v-model="form.active" label="Active account" color="primary" density="compact" hide-details />
              <v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ fieldHelp.active }}</v-tooltip>
            </div>
            <v-text-field v-if="showImapFields" v-model="form.host" label="Host" placeholder="imap.example.com" density="compact">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ fieldHelp.host }}</v-tooltip></template>
            </v-text-field>
            <v-text-field v-if="showImapFields" v-model="form.port" label="Port" placeholder="993" density="compact">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ fieldHelp.port }}</v-tooltip></template>
            </v-text-field>
            <v-text-field v-if="showUsername" v-model="form.username" label="Username" placeholder="me@example.com" density="compact">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ fieldHelp.username }}</v-tooltip></template>
            </v-text-field>
            <v-text-field v-model.number="form.sync_window_days" label="Only ingest last N days" type="number" placeholder="Blank = no limit" density="compact">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ fieldHelp.syncWindowDays }}</v-tooltip></template>
            </v-text-field>
            <div v-if="showImapFields" class="full-width">
              <v-switch v-model="form.ssl" label="Use SSL" color="primary" density="compact" hide-details />
              <v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ fieldHelp.ssl }}</v-tooltip>
            </div>
            <div v-if="showImapFields" class="full-width">
              <v-switch v-model="form.ignore_tls_errors" label="Ignore TLS errors" color="warning" density="compact" hide-details />
              <v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ fieldHelp.ignoreTlsErrors }}</v-tooltip>
            </div>
            <v-text-field v-if="showEws" v-model="form.endpoint" label="EWS endpoint" placeholder="https://exchange.example.com/EWS/Exchange.asmx" density="compact" class="full-width">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ fieldHelp.endpoint }}</v-tooltip></template>
            </v-text-field>
            <div class="form-actions full-width">
              <v-btn type="submit" color="primary" :loading="saving" prepend-icon="mdi-content-save">{{ accountSubmitLabel }}</v-btn>
              <v-btn v-if="accounts.length" variant="tonal" prepend-icon="mdi-close" @click.prevent="closeAccountForm">Cancel</v-btn>
              <span class="subtle">{{ status }}</span>
            </div>
          </v-form>
        </v-card>

        <v-card class="view-card wide-card" variant="flat">
          <div class="section-header">
            <div><h2>Saved Accounts</h2><p class="subtle">Choose mailboxes, then sync selected folders.</p></div>
            <div class="header-actions">
              <v-btn v-if="!showAccountForm" color="primary" prepend-icon="mdi-plus" @click="newAccount">Add account</v-btn>
              <v-btn variant="tonal" :loading="loadingAccounts" prepend-icon="mdi-refresh" @click="loadAccounts">Reload</v-btn>
            </div>
          </div>
          <p v-if="status && !showAccountForm" class="subtle">{{ status }}</p>
          <p v-if="!accounts.length" class="subtle">No mail accounts yet. Add the first account to start syncing mail.</p>
          <div class="stack">
            <v-card v-for="account in accounts" :key="account.id" class="entity-card" variant="flat">
              <v-card-title>{{ account.name }}</v-card-title>
              <v-card-subtitle>
                {{ providerLabel(account.provider_type) }} | {{ account.auth_mode }} |
                <v-chip size="x-small" :color="account.status === 'disabled' ? 'grey' : 'success'">{{ account.status || 'active' }}</v-chip>
                <v-chip size="x-small" :color="getStatusColor(account.sync_status)">sync {{ account.sync_status || 'idle' }}</v-chip>
              </v-card-subtitle>
              <v-card-text>
                <p>{{ accountSummary(account) }}</p>
                <div class="chip-row">
                  <v-chip size="small" variant="tonal">{{ account.auth_mode === 'google_oauth' ? (account.has_google_authorization ? 'Google authorized' : 'Google connection needed') : (account.has_password ? 'Password saved' : 'secret: ' + (account.secret_locator || 'none')) }}</v-chip>
                </div>
                <div v-if="accountMailboxes[account.id]" class="mailbox-picker">
                  <v-checkbox v-for="mailbox in accountMailboxes[account.id]" :key="mailbox.id" v-model="mailbox.monitored" :label="mailbox.name" density="compact" hide-details @change="updateMailbox(mailbox)" />
                </div>
              </v-card-text>
              <v-card-actions>
                <v-btn v-if="account.auth_mode === 'google_oauth'" color="primary" @click="connectGoogle(account)">{{ account.has_google_authorization ? 'Reconnect Google' : 'Connect Google' }}</v-btn>
                <GoogleOAuthSetup v-if="account.auth_mode === 'google_oauth'" :fetch-json="fetchJson" />
                <v-btn color="primary" prepend-icon="mdi-sync" @click="syncAccount(account)">Sync</v-btn>
                <v-btn variant="tonal" prepend-icon="mdi-pencil" @click="editAccount(account)">Edit</v-btn>
                <v-btn variant="tonal" prepend-icon="mdi-checkbox-marked-outline" @click="discoverMailboxes(account)">Choose mailboxes</v-btn>
                <v-btn color="error" variant="tonal" prepend-icon="mdi-delete-outline" @click="deleteAccount(account)">Delete</v-btn>
              </v-card-actions>
            </v-card>
          </div>
        </v-card>

        <v-card class="view-card wide-card" variant="flat">
          <div class="section-header">
            <div><h2>Progress</h2><p class="subtle">Sync, embedding, clustering, and reindex jobs.</p></div>
            <v-btn color="primary" :loading="loadingJobs" prepend-icon="mdi-refresh" @click="loadJobs">Reload</v-btn>
          </div>
          <p v-if="!jobs.length" class="subtle">No jobs yet.</p>
          <div class="stack">
            <v-card v-for="job in jobs" :key="job.id" class="entity-card" variant="flat">
              <v-card-title>{{ job.job_type }} | {{ job.status }}</v-card-title>
              <v-card-subtitle>{{ job.phase }} | {{ job.processed_count }}/{{ job.total_count || '?' }}</v-card-subtitle>
              <v-card-text>{{ job.error_text || job.current_item || 'No current item.' }}</v-card-text>
            </v-card>
          </div>
        </v-card>
      </div>
    </SettingsPage>
  `,
};

const SettingsPage = {
  props: ["title"],
  template: `
    <v-card class="settings-page-card" variant="flat">
      <div class="settings-page-header">
        <div>
          <p class="eyebrow">Settings</p>
          <h2>{{ title }}</h2>
        </div>
        <v-btn class="settings-close-button" icon="mdi-close" variant="text" to="/" aria-label="Close settings" title="Close settings" />
      </div>
      <slot></slot>
    </v-card>
  `,
};

const TriageSettingsView = {
  data() {
    return {
      user: defaultUserProfile(),
      interest: defaultInterestProfile(),
      profiles: [],
      userStatus: "",
      interestStatus: "",
      interestFormOpen: false,
    };
  },
  computed: {
    userFieldHelp() {
      return {
        name: "Your name or preferred display name. Helps the app interpret whether messages are directly addressed to you.",
        aliases: "Comma-separated alternate names, usernames, handles, and local-parts that refer to you. These are identity hints, not evidence.",
        emailAddresses: "Comma-separated email addresses that belong to you. These help distinguish mailbox-owner identity from destinations, senders, and subjects.",
        role: "Short description of your role, such as 'CERT analyst' or 'security manager'. Used as triage context for relevance and summaries.",
        organization: "Your team or organization name. Useful when messages refer to internal context, customers, or partner relationships.",
        collaborators: "Comma-separated people you work with often. Helps the app recognize relevant senders, recipients, and topics.",
        communicationPreferences: "How you prefer messages to be summarized or prioritized. Example: deadlines first, action items up front, newsletters de-emphasized.",
        about: "Plain-language description of your work, responsibilities, and what matters to you. This gives the strongest high-level triage context.",
      };
    },
    showInterestForm() {
      return this.interestFormOpen || !this.profiles.length;
    },
    interestFormTitle() {
      return this.interest.id ? "Edit Interest Profile" : "Add Interest Profile";
    },
    interestSubmitLabel() {
      return this.interest.id ? "Save changes" : "Add profile";
    },
  },
  mounted() {
    this.loadAll();
  },
  methods: {
    async loadAll() {
      await Promise.all([this.loadUserProfile(), this.loadProfiles()]);
    },
    async loadUserProfile() {
      const data = await fetchJson("/api/user-profile");
      const profile = data.profile || {};
      this.user = {
        name: profile.name || "",
        role: profile.role || "",
        organization: profile.organization || "",
        aliases: (profile.aliases_json || []).join(", "),
        email_addresses: (profile.email_addresses_json || []).join(", "),
        collaborators: (profile.collaborators_json || []).join(", "),
        communication_preferences: profile.communication_preferences || "",
        about: profile.about || "",
      };
    },
    async saveUserProfile() {
      this.userStatus = "Saving triage prompt...";
      try {
        await fetchJson("/api/user-profile", {
          method: "PUT",
          body: JSON.stringify({
            name: this.user.name.trim(),
            role: this.user.role.trim(),
            organization: this.user.organization.trim(),
            aliases: parseCsv(this.user.aliases),
            email_addresses: parseCsv(this.user.email_addresses),
            collaborators: parseCsv(this.user.collaborators),
            communication_preferences: this.user.communication_preferences.trim(),
            about: this.user.about.trim(),
          }),
        });
        this.userStatus = "Triage profile saved.";
      } catch (error) {
        this.userStatus = error.message;
      }
    },
    async loadProfiles() {
      const data = await fetchJson("/api/interest-profiles");
      this.profiles = data.interest_profiles || [];
      if (!this.profiles.length) this.interestFormOpen = true;
    },
    newProfile() {
      this.resetProfile();
      this.interestFormOpen = true;
      this.interestStatus = "";
    },
    editProfile(profile) {
      this.interest = {
        id: profile.id,
        name: profile.name || "",
        description: profile.description || "",
        collaborators: (profile.collaborators_json || []).join(", "),
        signals: profile.signals || "",
        priority_weight: profile.priority_weight ?? 1,
        dashboard_prominence: profile.dashboard_prominence ?? 1,
        summary_detail: profile.summary_detail || "standard",
        watch_mode: profile.watch_mode || "watch",
      };
      this.interestFormOpen = true;
      this.interestStatus = `Editing ${profile.name}.`;
    },
    resetProfile() {
      this.interest = defaultInterestProfile();
    },
    closeProfileForm() {
      this.resetProfile();
      this.interestFormOpen = false;
      this.interestStatus = "";
    },
    async saveProfile() {
      this.interestStatus = this.interest.id ? "Updating interest profile..." : "Saving interest profile...";
      try {
        await fetchJson("/api/interest-profiles", {
          method: "POST",
          body: JSON.stringify({
            id: this.interest.id,
            name: this.interest.name.trim(),
            description: this.interest.description.trim(),
            collaborators: parseCsv(this.interest.collaborators),
            signals: this.interest.signals.trim(),
            priority_weight: Number(this.interest.priority_weight || 1),
            dashboard_prominence: Number(this.interest.dashboard_prominence || 1),
            summary_detail: this.interest.summary_detail,
            watch_mode: this.interest.watch_mode,
          }),
        });
        this.interestStatus = "Interest profile saved.";
        this.resetProfile();
        await this.loadProfiles();
        this.interestFormOpen = !this.profiles.length;
      } catch (error) {
        this.interestStatus = error.message;
      }
    },
    async deleteProfile(profile = this.interest) {
      if (!profile.id || !confirm(`Delete ${profile.name || "this profile"}? This removes the saved profile and its existing matches.`)) return;
      try {
        await fetchJson(`/api/interest-profiles/${profile.id}`, { method: "DELETE" });
        this.interestStatus = `${profile.name || "Profile"} deleted.`;
        if (this.interest.id === profile.id) this.resetProfile();
        await this.loadProfiles();
        this.interestFormOpen = !this.profiles.length;
      } catch (error) {
        this.interestStatus = error.message;
      }
    },
  },
  template: `
    <SettingsPage title="Triage Settings">
      <div class="settings-layout">
        <v-card class="view-card" variant="flat">
          <div class="section-header"><div><h2>Triage Prompt</h2><p class="subtle">Tell MailIntel how to prioritize and summarize mail.</p></div></div>
          <v-form class="settings-form-vuetify" @submit.prevent="saveUserProfile">
            <v-text-field v-model="user.name" label="Name" density="compact">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ userFieldHelp.name }}</v-tooltip></template>
            </v-text-field>
            <v-text-field v-model="user.aliases" label="Aliases and usernames" density="compact">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ userFieldHelp.aliases }}</v-tooltip></template>
            </v-text-field>
            <v-text-field v-model="user.email_addresses" label="Your email addresses" density="compact">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ userFieldHelp.emailAddresses }}</v-tooltip></template>
            </v-text-field>
            <v-text-field v-model="user.role" label="Role" density="compact">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ userFieldHelp.role }}</v-tooltip></template>
            </v-text-field>
            <v-text-field v-model="user.organization" label="Organization" density="compact">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ userFieldHelp.organization }}</v-tooltip></template>
            </v-text-field>
            <v-text-field v-model="user.collaborators" label="Collaborators" class="full-width" density="compact">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ userFieldHelp.collaborators }}</v-tooltip></template>
            </v-text-field>
            <v-textarea v-model="user.communication_preferences" label="Communication preferences" class="full-width" rows="5">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ userFieldHelp.communicationPreferences }}</v-tooltip></template>
            </v-textarea>
            <v-textarea v-model="user.about" label="About your work" class="full-width" rows="5">
              <template #append-inner><v-tooltip location="top"><template #activator="{ props }"><v-icon v-bind="props" icon="mdi-information" /></template>{{ userFieldHelp.about }}</v-tooltip></template>
            </v-textarea>
            <div class="form-actions full-width"><v-btn type="submit" color="primary" prepend-icon="mdi-content-save">Save triage prompt</v-btn><span class="subtle">{{ userStatus }}</span></div>
          </v-form>
        </v-card>

        <v-card v-if="showInterestForm" class="view-card" variant="flat">
          <div class="section-header">
            <div><h2>{{ interestFormTitle }}</h2><p class="subtle">Create profile-driven triage weighting.</p></div>
            <v-btn v-if="profiles.length" class="settings-close-button" icon="mdi-close" variant="text" aria-label="Close profile form" title="Close profile form" @click="closeProfileForm" />
          </div>
          <v-form class="settings-form-vuetify" @submit.prevent="saveProfile">
            <v-text-field v-model="interest.name" label="Profile name" required density="compact" />
            <v-text-field v-model.number="interest.priority_weight" label="Priority weight" type="number" step="0.1" min="0" max="3" density="compact">
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  Multiplier applied to profile match scores (0–3). Higher values push matching messages higher in triage. Default is 1.
                </v-tooltip>
              </template>
            </v-text-field>
            <v-textarea v-model="interest.description" label="Description for LLM triage" class="full-width" rows="4">
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  Natural-language definition of what belongs in this profile. Used by the LLM as the main matching instruction.
                </v-tooltip>
              </template>
            </v-textarea>
            <v-textarea v-model="interest.signals" label="Fallback keyword signals" class="full-width" rows="4">
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  Comma-separated exact hints shown to the LLM as supporting evidence for profile matching.
                </v-tooltip>
              </template>
            </v-textarea>
            <v-text-field v-model="interest.collaborators" label="Collaborators" class="full-width" density="compact" />
            <v-text-field v-model.number="interest.dashboard_prominence" label="Dashboard prominence" type="number" min="1" max="5" density="compact">
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  Controls the display order of interest profiles on the dashboard (1–5). Higher values place the profile closer to the top.
                </v-tooltip>
              </template>
            </v-text-field>
            <v-select v-model="interest.summary_detail" label="Summary detail" :items="['standard','detailed','concise']" density="compact" />
            <v-select v-model="interest.watch_mode" label="Watch mode" :items="['watch','track','focus']" density="compact" />
            <div class="form-actions full-width">
              <v-btn type="submit" color="primary" prepend-icon="mdi-content-save">{{ interestSubmitLabel }}</v-btn>
              <v-btn v-if="interest.id" color="error" variant="tonal" prepend-icon="mdi-delete-outline" @click.prevent="deleteProfile()">Delete profile</v-btn>
              <v-btn v-if="profiles.length" variant="tonal" prepend-icon="mdi-close" @click.prevent="closeProfileForm">Cancel</v-btn>
              <span class="subtle">{{ interestStatus }}</span>
            </div>
          </v-form>
        </v-card>

        <v-card class="view-card wide-card" variant="flat">
          <div class="section-header">
            <div><h2>Saved Interest Profiles</h2><p class="subtle">Profiles used for scoring and collaborator matching.</p></div>
            <div class="header-actions">
              <v-btn v-if="!showInterestForm" color="primary" prepend-icon="mdi-plus" @click="newProfile">Add profile</v-btn>
              <v-btn variant="tonal" prepend-icon="mdi-refresh" @click="loadProfiles">Reload</v-btn>
            </div>
          </div>
          <p v-if="interestStatus && !showInterestForm" class="subtle">{{ interestStatus }}</p>
          <p v-if="!profiles.length" class="subtle">No interest profiles yet. Add the first profile to start profile-based triage.</p>
          <div class="stack">
            <v-card v-for="profile in profiles" :key="profile.id" class="entity-card" variant="flat">
              <v-card-title>{{ profile.name }}</v-card-title>
              <v-card-subtitle>{{ profile.watch_mode }} | {{ profile.summary_detail }} detail | weight {{ profile.priority_weight }}</v-card-subtitle>
              <v-card-text>{{ profile.description || 'No description.' }}<br />Signals: {{ profile.signals || 'none' }}<br />Collaborators: {{ (profile.collaborators_json || []).join(', ') || 'none' }}</v-card-text>
              <v-card-actions><v-btn variant="tonal" prepend-icon="mdi-pencil" @click="editProfile(profile)">Edit profile</v-btn><v-btn color="error" variant="tonal" prepend-icon="mdi-delete-outline" @click="deleteProfile(profile)">Delete profile</v-btn></v-card-actions>
            </v-card>
          </div>
        </v-card>
      </div>
    </SettingsPage>
  `,
};

const LlmSettingsView = {
  data() {
    return {
      presets: [],
      preset: defaultPreset(),
      pipelineSettings: { canonical_embedding_model: "nomic-embed-text" },
      capabilityOptions: PRESET_CAPABILITIES,
      concurrencyOptions: Array.from({length: 20}, (_, i) => i + 1),
      status: "",
      loading: false,
      saving: false,
      rebuilding: false,
      checking: false,
      presetFormOpen: false,
    };
  },
  computed: {
    fieldHelp() {
      return {
        name: "Human label for this preset. Use something like 'Local Ollama 8B' or 'OpenAI GPT-4.1'.",
        priority: "Lower numbers run first. Use 10, 20, 30 to leave space for reordering. Put the cheapest or most reliable preset first.",
        provider: "Select the API shape. Use Ollama for local models on your machine, OpenAI for OpenAI-compatible chat endpoints.",
        authMode: "How the app finds credentials. 'env' reads the variable named in Secret locator. Keep it as 'env' unless you add another auth flow in code.",
        secretLocator: "Environment variable name for the API key. Example: OPENAI_API_KEY. Leave empty for unauthenticated local Ollama.",
        endpointUrl: "Base URL for the provider. Examples: http://127.0.0.1:11434 for Ollama, https://api.openai.com/v1 for OpenAI.",
        textModel: "Optional model for all checked text tasks, including summaries, interest matching, topic labeling and search. Leave empty for an embedding-only preset.",
        embeddingModel: "Optional model used to build search embeddings. Leave empty for a text-only preset. Changing it may require rebuilding embeddings.",
        temperature: "Randomness control. 0.0-0.3 is best here for stable triage behavior; 0.2 is a sensible default.",
        textContextWindow: "Usable context per text request, including input, output allowance and prompt overhead. Match the provider configuration. Also sent to Ollama as num_ctx.",
        embeddingContextWindow: "Usable input context per embedding request, including provider-added tokens. Match the embedding server configuration independently of the text model.",
        maxOutputTokens: "Maximum generated output. 300-800 is enough for this app in most cases. Higher values waste time and can increase hallucinated verbosity.",
        initialTimeout: "Maximum wait for the first meaningful output from a streamed text request. Blank uses the server default. Does not apply to embeddings.",
        streamingTimeout: "Maximum quiet gap between meaningful output chunks. Blank uses the server default. Does not apply to embeddings.",
        enabled: "Only enabled presets are used. They run in ascending priority order until one succeeds.",
        notes: "Free-form notes for the user only. This field is not sent to the LLM and is not used in prompts, scoring, or retrieval. Good place for model size, GPU/RAM constraints, or why this preset exists.",
      };
    },
    showPresetForm() {
      return this.presetFormOpen || !this.presets.length;
    },
    presetFormTitle() {
      return this.preset.id ? "Edit LLM Preset" : "Add LLM Preset";
    },
    presetSubmitLabel() {
      return this.preset.id ? "Save changes" : "Add preset";
    },
    enabledPresetOptions() {
      return this.presets
        .filter((preset) => preset.enabled)
        .map((preset) => ({ title: preset.name, value: Number(preset.id) }));
    },
  },
  mounted() {
    this.loadPresets();
  },
  watch: {
    "preset.provider"(provider) {
      if (provider === "openai" && (!this.preset.endpoint_url || this.preset.endpoint_url === "http://127.0.0.1:11434")) {
        this.preset.endpoint_url = "https://api.openai.com/v1";
      }
      if (provider === "ollama" && (!this.preset.endpoint_url || this.preset.endpoint_url === "https://api.openai.com/v1")) {
        this.preset.endpoint_url = "http://127.0.0.1:11434";
      }
    },
  },
  methods: {
    async loadPresets() {
      this.loading = true;
      try {
        const [presetData, pipelineData] = await Promise.all([
          fetchJson("/api/llm-presets"),
          fetchJson("/api/pipeline-settings"),
        ]);
        this.presets = presetData.llm_presets || [];
        this.pipelineSettings = {
          ...this.pipelineSettings,
          ...(pipelineData.pipeline_settings || {}),
        };
        if (!this.presets.length) {
          this.preset = defaultPreset(this.presets);
          this.presetFormOpen = true;
        } else if (this.preset.id) {
          const selected = this.presets.find((item) => Number(item.id) === Number(this.preset.id));
          if (selected) this.preset = { ...selected, capabilities: [...(selected.capabilities_json ?? selected.capabilities ?? [])] };
        }
      } catch (error) {
        this.status = error.message;
      } finally {
        this.loading = false;
      }
    },
    newPreset() {
      this.preset = defaultPreset(this.presets);
      this.presetFormOpen = true;
      this.status = "";
    },
    editPreset(preset) {
      this.preset = { ...preset, capabilities: [...(preset.capabilities_json ?? preset.capabilities ?? PRESET_CAPABILITIES.map(item => item.value))] };
      this.presetFormOpen = true;
      this.status = `Editing ${preset.name}.`;
    },
    closePresetForm() {
      this.preset = defaultPreset(this.presets);
      this.presetFormOpen = false;
      this.status = "";
    },
    async savePreset() {
      if (!this.preset.name.trim()) {
        this.status = "Preset name is required.";
        return;
      }
      this.saving = true;
      try {
        const preset = {
          ...this.preset,
          initial_timeout_seconds: this.preset.initial_timeout_seconds === "" ? null : this.preset.initial_timeout_seconds,
          streaming_timeout_seconds: this.preset.streaming_timeout_seconds === "" ? null : this.preset.streaming_timeout_seconds,
        };
        const data = await fetchJson("/api/llm-presets", { method: "POST", body: JSON.stringify(preset) });
        await this.loadPresets();
        const saved = this.presets.find((item) => Number(item.id) === Number(data.llm_preset_id));
        if (saved) this.preset = { ...saved, capabilities: [...(saved.capabilities_json ?? saved.capabilities ?? [])] };
        this.presetFormOpen = !this.presets.length;
        this.status = Number(data.stale_embedding_count || 0)
          ? `LLM preset saved. ${data.stale_embedding_count} existing embeddings are stale and should be rebuilt.`
          : "LLM preset saved.";
      } catch (error) {
        this.status = error.message;
      } finally {
        this.saving = false;
      }
    },
    async savePipelineSettings() {
      try {
        const data = await fetchJson("/api/pipeline-settings", {
          method: "POST",
          body: JSON.stringify(this.pipelineSettings),
        });
        this.pipelineSettings = {
          ...this.pipelineSettings,
          ...(data.pipeline_settings || {}),
        };
        this.status = "Pipeline settings saved.";
      } catch (error) {
        this.status = error.message;
      }
    },
    async deletePreset(preset = this.preset) {
      if (!preset.id || !confirm(`Delete LLM preset "${preset.name}"?`)) return;
      try {
        await fetchJson(`/api/llm-presets/${preset.id}`, { method: "DELETE" });
        this.status = "Preset deleted.";
        this.preset = defaultPreset(this.presets);
        await this.loadPresets();
        this.presetFormOpen = !this.presets.length;
      } catch (error) {
        this.status = error.message;
      }
    },
    async movePreset(index, offset) {
      const target = index + offset;
      if (target < 0 || target >= this.presets.length) return;
      const reordered = [...this.presets];
      const [preset] = reordered.splice(index, 1);
      reordered.splice(target, 0, preset);
      const priorities = reordered.map((item, priorityIndex) => ({ id: item.id, priority: (priorityIndex + 1) * 10 }));
      try {
        const data = await fetchJson("/api/llm-presets/reorder", { method: "POST", body: JSON.stringify({ presets: priorities }) });
        this.presets = data.llm_presets || [];
        const selected = this.presets.find((item) => item.id === preset.id);
        if (selected) this.preset = { ...selected, capabilities: [...(selected.capabilities_json ?? selected.capabilities ?? [])] };
        this.status = "Preset order saved.";
      } catch (error) {
        this.status = error.message;
      }
    },
    async rebuildEmbeddings() {
      this.rebuilding = true;
      this.status = "Starting search embedding rebuild...";
      try {
        const data = await fetchJson("/api/rebuild-embeddings", { method: "POST", body: JSON.stringify({ limit: 1000 }) });
        this.status = `Search embedding rebuild scheduled as job #${data.job_id || "unknown"} (${data.marked_messages || 0} messages, ${data.marked_interval_summaries || 0} summaries marked).`;
      } catch (error) {
        this.status = error.message;
      } finally {
        this.rebuilding = false;
      }
    },
    async checkModel() {
      this.checking = true;
      this.status = "Queueing saved preset check...";
      try {
        const data = await fetchJson("/api/llm-presets/check", { method: "POST", body: JSON.stringify(this.preset) });
        this.status = `Saved preset check queued as job #${data.job_id}. See Troubleshooting for its result.`;
      } catch (error) {
        this.status = `Model check failed: ${error.message}`;
      } finally {
        this.checking = false;
      }
    },
  },
  template: `
    <SettingsPage title="LLM Settings">
      <div class="settings-layout">
        <v-card class="view-card" variant="flat">
          <div class="section-header"><div><h2>Pipeline Settings</h2><p class="subtle">Choose how analysis and embedding workers select presets.</p></div></div>
          <v-form class="settings-form-vuetify" @submit.prevent="savePipelineSettings">
            <v-text-field v-model="pipelineSettings.canonical_embedding_model" label="Canonical embedding model" density="compact" />
            <p class="subtle full-width">Jobs use the allowed capabilities and preset priority. A healthy higher-priority compatible preset takes precedence even when busy.</p>
            <div class="form-actions full-width">
              <v-btn type="submit" color="primary" prepend-icon="mdi-content-save">Save pipeline settings</v-btn>
            </div>
          </v-form>
        </v-card>

        <v-card v-if="showPresetForm" class="view-card" variant="flat">
          <div class="section-header">
            <div><h2>{{ presetFormTitle }}</h2><p class="subtle">Configure one provider preset.</p></div>
            <v-btn v-if="presets.length" class="settings-close-button" icon="mdi-close" variant="text" aria-label="Close preset form" title="Close preset form" @click="closePresetForm" />
          </div>
          <v-form class="settings-form-vuetify" @submit.prevent="savePreset">
            <v-text-field
              v-model="preset.name"
              label="Preset name"
              required
              density="compact"
            >
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  {{ fieldHelp.name }}
                </v-tooltip>
              </template>
            </v-text-field>
            <v-text-field v-model.number="preset.priority" label="Priority" type="number" min="1" density="compact">
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  {{ fieldHelp.priority }}
                </v-tooltip>
              </template>
            </v-text-field>
            <v-select v-model="preset.concurrency" label="Concurrency" :items="concurrencyOptions" density="compact" hint="Total simultaneous calls shared by generation and embeddings" persistent-hint />
            <div class="full-width">
              <p>Usable for</p>
              <v-checkbox v-for="item in capabilityOptions" :key="item.value" v-model="preset.capabilities" :value="item.value" :label="item.title" :disabled="!String(preset[item.model] || '').trim()" density="compact" hide-details />
            </div>
            <v-select v-model="preset.provider" label="Provider" :items="[{title:'Ollama',value:'ollama'},{title:'OpenAI',value:'openai'}]" density="compact">
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  {{ fieldHelp.provider }}
                </v-tooltip>
              </template>
            </v-select>
            <v-text-field v-model="preset.auth_mode" label="Auth mode" density="compact">
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  {{ fieldHelp.authMode }}
                </v-tooltip>
              </template>
            </v-text-field>
            <v-text-field v-model="preset.secret_locator" label="Secret locator" :placeholder="preset.provider === 'openai' ? 'OPENAI_API_KEY' : 'Optional for local Ollama'" density="compact">
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  {{ fieldHelp.secretLocator }}
                </v-tooltip>
              </template>
            </v-text-field>
            <v-text-field v-model="preset.endpoint_url" label="Endpoint URL" density="compact">
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  {{ fieldHelp.endpointUrl }}
                </v-tooltip>
              </template>
            </v-text-field>
            <v-text-field v-model="preset.text_model" label="Text model (optional)" density="compact">
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  {{ fieldHelp.textModel }}
                </v-tooltip>
              </template>
            </v-text-field>
            <v-text-field v-model.number="preset.text_context_window" label="Text context size" type="number" min="1" density="compact" :disabled="!String(preset.text_model || '').trim()" hint="Tokens per request, including input and output" persistent-hint>
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  {{ fieldHelp.textContextWindow }}
                </v-tooltip>
              </template>
            </v-text-field>
            <v-text-field v-model="preset.embedding_model" label="Embedding model (optional)" density="compact">
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  {{ fieldHelp.embeddingModel }}
                </v-tooltip>
              </template>
            </v-text-field>
            <v-text-field v-model.number="preset.embedding_context_window" label="Embedding context size" type="number" min="1" density="compact" :disabled="!String(preset.embedding_model || '').trim()" hint="Input tokens per request, including provider overhead" persistent-hint>
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  {{ fieldHelp.embeddingContextWindow }}
                </v-tooltip>
              </template>
            </v-text-field>
            <v-text-field v-model.number="preset.temperature" label="Temperature" type="number" step="0.1" min="0" max="2" density="compact">
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  {{ fieldHelp.temperature }}
                </v-tooltip>
              </template>
            </v-text-field>
            <v-text-field v-model.number="preset.max_output_tokens" label="Max output tokens" type="number" min="1" density="compact">
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  {{ fieldHelp.maxOutputTokens }}
                </v-tooltip>
              </template>
            </v-text-field>
            <v-text-field v-model.number="preset.initial_timeout_seconds" label="First output timeout (seconds, optional)" type="number" min="1" max="86400" step="1" density="compact" :disabled="!String(preset.text_model || '').trim()" :hint="fieldHelp.initialTimeout" persistent-hint />
            <v-text-field v-model.number="preset.streaming_timeout_seconds" label="Streaming gap timeout (seconds, optional)" type="number" min="1" max="86400" step="1" density="compact" :disabled="!String(preset.text_model || '').trim()" :hint="fieldHelp.streamingTimeout" persistent-hint />
            <div class="full-width">
              <v-switch v-model="preset.enabled" label="Enable this preset" color="success" density="compact" hide-details />
              <v-tooltip location="top">
                <template #activator="{ props }">
                  <v-icon v-bind="props" icon="mdi-information" />
                </template>
                {{ fieldHelp.enabled }}
              </v-tooltip>
            </div>
            <v-textarea v-model="preset.notes" label="Notes" rows="3" class="full-width">
              <template #append-inner>
                <v-tooltip location="top">
                  <template #activator="{ props }">
                    <v-icon v-bind="props" icon="mdi-information" />
                  </template>
                  {{ fieldHelp.notes }}
                </v-tooltip>
              </template>
            </v-textarea>
            <div class="form-actions full-width">
              <v-btn type="submit" color="primary" :loading="saving" prepend-icon="mdi-content-save">{{ presetSubmitLabel }}</v-btn>
              <v-btn v-if="presets.length" variant="tonal" prepend-icon="mdi-close" @click.prevent="closePresetForm">Cancel</v-btn>
              <v-btn v-if="preset.id" color="error" variant="tonal" prepend-icon="mdi-delete-outline" @click.prevent="deletePreset()">Delete preset</v-btn>
              <v-btn variant="tonal" :loading="checking" :disabled="!preset.id || !preset.enabled" prepend-icon="mdi-connection" @click.prevent="checkModel">Check saved model</v-btn>
              <v-btn variant="tonal" :loading="rebuilding" prepend-icon="mdi-vector-polyline" @click.prevent="rebuildEmbeddings">Rebuild search embeddings</v-btn>
              <span class="subtle">{{ status }}</span>
            </div>
          </v-form>
        </v-card>

        <v-card class="view-card" variant="flat">
          <div class="section-header">
            <div><h2>Saved Presets</h2><p class="subtle">Enabled presets run in order.</p></div>
            <div class="header-actions">
              <v-btn v-if="!showPresetForm" color="primary" prepend-icon="mdi-plus" @click="newPreset">Add preset</v-btn>
              <v-btn variant="tonal" :loading="loading" prepend-icon="mdi-refresh" @click="loadPresets">Reload</v-btn>
            </div>
          </div>
          <p v-if="status && !showPresetForm" class="subtle">{{ status }}</p>
          <p v-if="!presets.length" class="subtle">No LLM presets saved. Add the first preset to enable AI analysis.</p>
          <div class="stack">
            <v-card v-for="(item, index) in presets" :key="item.id" class="preset-card" :class="item.enabled ? 'is-enabled' : 'is-disabled'" variant="flat">
              <div class="preset-rail"></div>
              <div class="preset-content">
                <div class="preset-card-header">
                  <div>
                    <div class="preset-title">{{ item.name || 'Unnamed preset' }} <v-chip size="x-small" variant="tonal">{{ item.concurrency || 1 }}x</v-chip></div>
                    <div class="preset-meta">#{{ index + 1 }} | {{ item.provider }} | priority {{ item.priority }}</div>
                  </div>
                  <v-chip :color="item.enabled ? 'success' : 'grey'" size="small" variant="flat">{{ item.enabled ? 'Enabled' : 'Disabled' }}</v-chip>
                </div>
                <div class="preset-endpoint">{{ item.endpoint_url || 'No endpoint configured' }}</div>
                <div class="chip-row">
                  <v-chip v-if="item.text_model" size="small" variant="tonal">Text: {{ item.text_model }} · {{ item.text_context_window }} tokens</v-chip>
                  <v-chip v-if="item.embedding_model" size="small" variant="tonal">Embedding: {{ item.embedding_model }} · {{ item.embedding_context_window }} tokens</v-chip>
                </div>
                <p class="preset-notes">{{ item.notes || 'No notes.' }}</p>
                <div class="inline-actions">
                  <v-btn variant="tonal" size="small" prepend-icon="mdi-pencil" @click="editPreset(item)">Edit</v-btn>
                  <v-btn variant="tonal" size="small" prepend-icon="mdi-arrow-up" :disabled="index === 0" @click="movePreset(index, -1)">Up</v-btn>
                  <v-btn variant="tonal" size="small" prepend-icon="mdi-arrow-down" :disabled="index === presets.length - 1" @click="movePreset(index, 1)">Down</v-btn>
                  <v-btn color="error" variant="tonal" size="small" prepend-icon="mdi-delete-outline" @click="deletePreset(item)">Delete</v-btn>
                </div>
              </div>
            </v-card>
          </div>
        </v-card>
      </div>
    </SettingsPage>
  `,
};

const MaintenanceSettingsView = {
  data() {
    return {
      phrases: {
        reset_processed: "reset emails",
        interest_matches: "rebuild matches",
        topics: "rebuild topics",
        summaries: "reset summaries",
        customization: "remove customization",
        ai_settings: "remove ai settings",
      },
      sources: [],
      selectedSourceIds: [],
      resetPhrase: "",
      matchesPhrase: "",
      topicPhrase: "",
      summariesPhrase: "",
      customizationPhrase: "",
      aiPhrase: "",
      status: "",
      loading: false,
      resetting: false,
      rebuildingMatches: false,
      rebuildingTopics: false,
      resettingSummaries: false,
      removingCustomization: false,
      removingAi: false,
    };
  },
  computed: {
    canResetProcessed() {
      return this.selectedSourceIds.length > 0 && this.resetPhrase.trim() === this.phrases.reset_processed;
    },
    canRemoveCustomization() {
      return this.customizationPhrase.trim() === this.phrases.customization;
    },
    canRebuildInterestMatches() {
      return this.matchesPhrase.trim() === this.phrases.interest_matches;
    },
    canRebuildTopicCategories() {
      return this.topicPhrase.trim() === this.phrases.topics;
    },
    canResetSummaries() {
      return this.summariesPhrase.trim() === this.phrases.summaries;
    },
    canRemoveAi() {
      return this.aiPhrase.trim() === this.phrases.ai_settings;
    },
  },
  mounted() {
    this.loadMaintenance();
  },
  methods: {
    sourceMeta(source) {
      return `${source.provider_type || "mail"} | ${source.status || "unknown"}`;
    },
    async loadMaintenance() {
      this.loading = true;
      try {
        const data = await fetchJson("/api/maintenance");
        this.sources = data.sources || [];
        const validIds = new Set(this.sources.map((source) => Number(source.id)));
        this.selectedSourceIds = this.selectedSourceIds.filter((id) => validIds.has(Number(id)));
      } catch (error) {
        this.status = error.message;
      } finally {
        this.loading = false;
      }
    },
    async resetProcessed() {
      if (!this.canResetProcessed) return;
      this.resetting = true;
      this.status = "Resetting processed emails and starting refresh...";
      try {
        const data = await fetchJson("/api/maintenance/reset-processed", {
          method: "POST",
          body: JSON.stringify({ source_ids: this.selectedSourceIds }),
        });
        const errors = (data.sync_results || []).filter((item) => item.status === "error");
        const cleanup = `Deleted ${data.deleted_messages || 0} emails, ${data.deleted_interval_summaries || 0} mailbox summar${data.deleted_interval_summaries === 1 ? "y" : "ies"}, .`;
        this.status = errors.length
          ? `${cleanup} ${errors.length} source refresh failed: ${errors.map((item) => item.detail).join("; ")}`
          : `${cleanup} Refresh completed for ${this.selectedSourceIds.length} source(s).`;
        this.resetPhrase = "";
      } catch (error) {
        this.status = error.message;
      } finally {
        this.resetting = false;
      }
    },
    async removeCustomization() {
      if (!this.canRemoveCustomization) return;
      this.removingCustomization = true;
      try {
        const data = await fetchJson("/api/maintenance/remove-customization", {
          method: "POST",
          body: JSON.stringify({}),
        });
        this.status = `Removed ${data.deleted_interest_profiles || 0} interest profile(s) and ${data.deleted_user_profiles || 0} triage profile row(s).`;
        this.customizationPhrase = "";
      } catch (error) {
        this.status = error.message;
      } finally {
        this.removingCustomization = false;
      }
    },
    async rebuildInterestMatches() {
      if (!this.canRebuildInterestMatches) return;
      this.rebuildingMatches = true;
      try {
        const data = await fetchJson("/api/rebuild-interest-matches", {
          method: "POST",
          body: JSON.stringify({}),
        });
        this.status = `Scheduled AI analysis refresh for ${data.marked_messages || data.rebuilt_messages || 0} stored message(s).`;
        this.matchesPhrase = "";
        if (this.$root && typeof this.$root.loadStatus === "function") {
          this.$root.statusFastPollsRemaining = 8;
          await this.$root.loadStatus();
        }
      } catch (error) {
        this.status = error.message;
      } finally {
        this.rebuildingMatches = false;
      }
    },
    async rebuildTopicCategories() {
      if (!this.canRebuildTopicCategories) return;
      this.rebuildingTopics = true;
      try {
        const data = await fetchJson("/api/maintenance/rebuild-topics", {
          method: "POST",
          body: JSON.stringify({}),
        });
        const rebuild = data.rebuild || {};
        this.status = `Scheduled topic rebuild after clearing ${rebuild.topic_clusters || 0} cluster${rebuild.topic_clusters === 1 ? "" : "s"}, and ${rebuild.topic_memberships || 0} message membership${rebuild.topic_memberships === 1 ? "" : "s"}.`;
        this.topicPhrase = "";
        if (this.$root && typeof this.$root.loadStatus === "function") {
          this.$root.statusFastPollsRemaining = 8;
          await this.$root.loadStatus();
        }
      } catch (error) {
        this.status = error.message;
      } finally {
        this.rebuildingTopics = false;
      }
    },
    async resetSummaries() {
      if (!this.canResetSummaries) return;
      this.resettingSummaries = true;
      try {
        const data = await fetchJson("/api/maintenance/reset-summaries", {
          method: "POST",
          body: JSON.stringify({}),
        });
        this.status = `Deleted ${data.deleted_summaries || 0} mailbox summary row(s).`;
        this.summariesPhrase = "";
        if (this.$root && typeof this.$root.loadStatus === "function") {
          this.$root.statusFastPollsRemaining = 8;
          await this.$root.loadStatus();
        }
      } catch (error) {
        this.status = error.message;
      } finally {
        this.resettingSummaries = false;
      }
    },
    async removeAiSettings() {
      if (!this.canRemoveAi) return;
      this.removingAi = true;
      try {
        const data = await fetchJson("/api/maintenance/remove-ai-settings", {
          method: "POST",
          body: JSON.stringify({}),
        });
        this.status = `Removed ${data.deleted_llm_presets || 0} AI preset(s).`;
        this.aiPhrase = "";
      } catch (error) {
        this.status = error.message;
      } finally {
        this.removingAi = false;
      }
    },
  },
  template: `
    <SettingsPage title="Maintenance">
      <div class="settings-layout maintenance-layout">
        <v-card class="view-card wide-card" variant="flat">
          <div class="section-header">
            <div><h2>Processed Emails</h2><p class="subtle">Reset selected sources and immediately refresh them.</p></div>
            <v-btn color="primary" :loading="loading" prepend-icon="mdi-refresh" @click="loadMaintenance">Reload</v-btn>
          </div>
          <p v-if="!sources.length" class="subtle">No sources configured.</p>
          <div class="maintenance-source-list">
            <v-checkbox
              v-for="source in sources"
              :key="source.id"
              v-model="selectedSourceIds"
              :value="source.id"
              :label="source.name"
              :hint="sourceMeta(source)"
              persistent-hint
              density="compact"
            />
          </div>
          <div class="maintenance-confirm-row">
            <v-text-field v-model="resetPhrase" :label="'Type ' + phrases.reset_processed" density="compact" />
            <v-btn color="error" :disabled="!canResetProcessed" :loading="resetting" prepend-icon="mdi-email-sync-outline" @click="resetProcessed">Reset and refresh</v-btn>
          </div>
        </v-card>

        <v-card class="view-card" variant="flat">
          <div class="section-header"><div><h2>Interest Matches</h2><p class="subtle">Restart AI analysis and profile matching for stored emails without deleting or reingesting them.</p></div></div>
          <div class="maintenance-confirm-row">
            <v-text-field v-model="matchesPhrase" :label="'Type ' + phrases.interest_matches" density="compact" />
            <v-btn color="primary" :disabled="!canRebuildInterestMatches" :loading="rebuildingMatches" prepend-icon="mdi-account-search-outline" @click="rebuildInterestMatches">Rebuild matches</v-btn>
          </div>
        </v-card>

        <v-card class="view-card" variant="flat">
          <div class="section-header"><div><h2>Topics</h2><p class="subtle">Clear topics and rebuild clustering from canonical embeddings.</p></div></div>
          <div class="maintenance-confirm-row">
            <v-text-field v-model="topicPhrase" :label="'Type ' + phrases.topics" density="compact" />
            <v-btn color="error" :disabled="!canRebuildTopicCategories" :loading="rebuildingTopics" prepend-icon="mdi-graph-outline" @click="rebuildTopicCategories">Rebuild topics</v-btn>
          </div>
        </v-card>

        <v-card class="view-card" variant="flat">
          <div class="section-header"><div><h2>Mailbox Summaries</h2><p class="subtle">Delete generated daily, weekly, and monthly mailbox summaries. The summary worker will recreate rows from summarized mail.</p></div></div>
          <div class="maintenance-confirm-row">
            <v-text-field v-model="summariesPhrase" :label="'Type ' + phrases.summaries" density="compact" />
            <v-btn color="error" :disabled="!canResetSummaries" :loading="resettingSummaries" prepend-icon="mdi-calendar-remove-outline" @click="resetSummaries">Reset summaries</v-btn>
          </div>
        </v-card>

        <v-card class="view-card" variant="flat">
          <div class="section-header"><div><h2>Customization Settings</h2><p class="subtle">Remove the triage profile and saved interest profiles.</p></div></div>
          <div class="maintenance-confirm-row">
            <v-text-field v-model="customizationPhrase" :label="'Type ' + phrases.customization" density="compact" />
            <v-btn color="error" :disabled="!canRemoveCustomization" :loading="removingCustomization" prepend-icon="mdi-account-remove-outline" @click="removeCustomization">Remove customization</v-btn>
          </div>
        </v-card>

        <v-card class="view-card" variant="flat">
          <div class="section-header"><div><h2>AI Settings</h2><p class="subtle">Remove saved LLM provider presets.</p></div></div>
          <div class="maintenance-confirm-row">
            <v-text-field v-model="aiPhrase" :label="'Type ' + phrases.ai_settings" density="compact" />
            <v-btn color="error" :disabled="!canRemoveAi" :loading="removingAi" prepend-icon="mdi-robot-off-outline" @click="removeAiSettings">Remove AI settings</v-btn>
          </div>
        </v-card>

        <v-alert v-if="status" class="wide-card" variant="tonal" density="compact" :type="status.startsWith('Request failed') ? 'error' : 'info'">{{ status }}</v-alert>
      </div>
    </SettingsPage>
  `,
};

const TroubleshootingView = {
  data() {
    return {
      jobs: [], workers: [], events: [], loading: false, error: "",
      queueState: "", queueQuery: "", queueKind: "", queueKinds: [], queueOffset: 0, queueTotal: 0, queueLimit: 100, changingJob: null,
      filters: {job_id: "", worker_id: "", preset_id: "", source_id: "", action: ""},
      actions: ["queued", "accepted", "completed", "requeued", "paused", "resumed", "reprioritized", "deleted", "cancelled", "started", "stopped", "unavailable", "recovered"],
    };
  },
  mounted() { this.reload(); },
  methods: {
    async reload() {
      this.loading = true;
      try {
        const params = new URLSearchParams(Object.entries(this.filters).filter(([,value]) => value !== "" && value !== null));
        const queueParams = new URLSearchParams(params);
        queueParams.delete("action");
        queueParams.set("limit", this.queueLimit);
        queueParams.set("offset", this.queueOffset);
        if (this.queueState) queueParams.set("state", this.queueState);
        if (this.queueQuery?.trim()) queueParams.set("q", this.queueQuery.trim());
        if (this.queueKind) queueParams.set("kind", this.queueKind);
        const [data, queue] = await Promise.all([
          fetchJson(`/api/troubleshooting?${params}`),
          fetchJson(`/api/work-queue?${queueParams}`),
        ]);
        this.jobs = (queue.jobs || []).map((job) => ({...job, editedPriority: job.priority}));
        this.queueTotal = queue.total || 0;
        this.queueKinds = queue.kinds || [];
        this.workers = data.workers || [];
        this.events = data.events || [];
        this.error = "";
      } catch (error) { this.error = error.message; }
      finally { this.loading = false; }
    },
    applyFilters() { this.queueOffset = 0; this.reload(); },
    changePage(direction) { this.queueOffset = Math.max(0, this.queueOffset + direction * this.queueLimit); this.reload(); },
    async changeJob(job, action) {
      this.changingJob = job.id;
      try {
        const options = action === "priority"
          ? {method: "PATCH", body: JSON.stringify({priority: Number(job.editedPriority)})}
          : {method: action === "delete" ? "DELETE" : "POST"};
        await fetchJson(`/api/work-queue/${job.id}${action === "delete" ? "" : `/${action}`}`, options);
        await this.reload();
      } catch (error) { this.error = error.message; }
      finally { this.changingJob = null; }
    },
    timestamp(value) { return value ? new Date(value).toLocaleString() : ""; },
    jobLabel(kind) {
      return {
        analyze_message: "Mail summary", embed_message: "Mail embedding", interval_daily: "Daily summary",
        interval_rollup: "Weekly / monthly summary", embed_interval: "Summary embedding",
        cluster_message: "Mail clustering", label_cluster: "Cluster label", cluster_publish: "Publish cluster",
        relabel_topic: "Rename topic with AI", preset_check: "Preset check",
      }[kind] || kind.replaceAll("_", " ");
    },
    workerLabel(worker) {
      if (worker.worker_id === "discovery") return "Discovery (queue producer)";
      if (worker.worker_id === "local") return "Local jobs (no LLM)";
      if (worker.preset_name) return `${worker.preset_name} · slot ${worker.worker_id.split(":").at(-1)}`;
      return worker.source_name || worker.worker_id;
    },
  },
  template: `
    <SettingsPage title="Troubleshooting">
      <v-card class="view-card wide-card" variant="flat">
        <div class="section-header"><div><h2>Queue and workers</h2><p class="subtle">Completed jobs are deleted. The latest 5,000 events are retained.</p></div>
          <v-btn :loading="loading" prepend-icon="mdi-refresh" @click="reload">Reload</v-btn>
        </div>
        <v-alert v-if="error" type="error">{{ error }}</v-alert>
        <h3>Current queue</h3>
        <v-form class="settings-form-vuetify" @submit.prevent="applyFilters">
          <v-text-field v-model="queueQuery" label="Search queue" placeholder="Mail text, subject, sender, search question or job details" maxlength="200" density="compact" clearable />
          <v-select v-model="queueKind" label="Job type" :items="[{title: 'All types', value: ''}, ...queueKinds.map(kind => ({title: jobLabel(kind), value: kind}))]" density="compact" />
          <v-select v-model="queueState" label="State" :items="[{title: 'All states', value: ''}, {title: 'New', value: 'new'}, {title: 'Processing', value: 'processing'}, {title: 'Paused', value: 'paused'}]" density="compact" />
          <v-btn type="submit" :loading="loading" prepend-icon="mdi-magnify">Search</v-btn>
        </v-form>
        <p class="subtle">Search and filters apply to the entire queue before pagination. Job, worker, preset and source filters below also apply here.</p>
        <p class="subtle">Lower priorities run first: 0 manual override, 1 search, 2 individual mail, 3 daily summaries, 4 weekly/monthly summaries and interval embeddings, 5 clustering. Pause keeps background work blocked until resumed, including after a restart. Unfinished searches are discarded on restart. Deleted unfinished work may be queued again; deleting a search job stops that search.</p>
        <v-table density="compact"><thead><tr><th>Job</th><th>Type / preview</th><th>State</th><th>Priority</th><th>Retries</th><th>Preset / source</th><th>Worker</th><th>Actions</th></tr></thead>
          <tbody><tr v-for="job in jobs" :key="job.id">
            <td>{{ job.id }}</td><td style="min-width: 300px; width: 400px"><div :title="job.kind">{{ jobLabel(job.kind) }}</div>
              <v-textarea :model-value="job.preview || 'No preview available.'" readonly rows="3" density="compact" variant="outlined" hide-details :aria-label="'Preview of job ' + job.id" />
            </td><td>{{ job.state }}</td>
            <td><div class="d-flex align-center ga-1">
              <v-text-field v-model="job.editedPriority" type="number" min="0" step="1" density="compact" hide-details aria-label="Job priority" style="width: 84px" :disabled="changingJob !== null" />
              <v-btn icon="mdi-content-save-outline" size="small" variant="text" title="Save priority" :disabled="changingJob !== null || Number(job.editedPriority) === job.priority || !Number.isInteger(Number(job.editedPriority)) || Number(job.editedPriority) < 0 || job.editedPriority === ''" @click="changeJob(job, 'priority')" />
            </div></td><td>{{ job.retries }}</td><td>{{ job.preset_name || '' }}<br v-if="job.preset_name && job.source_name" />{{ job.source_name || '' }}</td><td>{{ job.worker_id || '' }}</td>
            <td class="text-no-wrap">
              <v-btn size="small" variant="text" :prepend-icon="job.state === 'paused' ? 'mdi-play' : 'mdi-pause'" :disabled="changingJob !== null" @click="changeJob(job, job.state === 'paused' ? 'resume' : 'pause')">{{ job.state === 'paused' ? 'Resume' : 'Pause' }}</v-btn>
              <v-btn size="small" variant="text" color="error" prepend-icon="mdi-delete-outline" :disabled="changingJob !== null" @click="changeJob(job, 'delete')">Delete</v-btn>
            </td>
          </tr></tbody>
        </v-table><p v-if="!jobs.length" class="subtle">No queued work.</p>
        <div class="d-flex align-center ga-2 my-3">
          <v-btn size="small" :disabled="loading || queueOffset === 0" @click="changePage(-1)">Previous</v-btn>
          <span class="subtle">{{ queueTotal ? queueOffset + 1 : 0 }}–{{ queueOffset + jobs.length }} of {{ queueTotal }}</span>
          <v-btn size="small" :disabled="loading || queueOffset + queueLimit >= queueTotal" @click="changePage(1)">Next</v-btn>
        </div>
        <h3>Workers</h3>
        <v-table density="compact"><thead><tr><th>Worker</th><th>State</th><th>Preset</th><th>Source</th><th>Updated</th><th>Reason</th></tr></thead>
          <tbody><tr v-for="worker in workers" :key="worker.worker_id"><td :title="worker.worker_id">{{ workerLabel(worker) }}</td><td>{{ worker.state }}</td><td>{{ worker.preset_name || '' }}</td><td>{{ worker.source_name || '' }}</td><td>{{ timestamp(worker.changed_at) }}</td><td>{{ worker.reason }}</td></tr></tbody>
        </v-table>
        <h3>Recent events</h3>
        <v-form class="settings-form-vuetify" @submit.prevent="applyFilters">
          <v-text-field v-model="filters.job_id" label="Job ID" type="number" density="compact" />
          <v-text-field v-model="filters.worker_id" label="Worker" density="compact" />
          <v-text-field v-model="filters.preset_id" label="Preset ID" type="number" density="compact" />
          <v-text-field v-model="filters.source_id" label="Source ID" type="number" density="compact" />
          <v-select v-model="filters.action" label="Action" :items="actions" clearable density="compact" />
          <v-btn type="submit" :loading="loading">Apply filters</v-btn>
        </v-form>
        <v-table density="compact"><thead><tr><th>Time</th><th>Action</th><th>Job / type</th><th>Worker</th><th>Preset / source</th><th>Retries</th><th>Reason</th></tr></thead>
          <tbody><tr v-for="event in events" :key="event.id"><td>{{ timestamp(event.occurred_at) }}</td><td>{{ event.action }}</td><td>{{ event.job_id || '' }} {{ event.kind || '' }}</td><td>{{ event.worker_id || '' }}</td><td>{{ event.preset_id || '' }} / {{ event.source_id || '' }}</td><td>{{ event.retries ?? '' }}</td><td>{{ event.reason }}</td></tr></tbody>
        </v-table><p v-if="!events.length" class="subtle">No matching events.</p>
      </v-card>
    </SettingsPage>
  `,
};

const routes = [
  { path: "/", component: HomeView },
  { path: "/mail-settings", component: MailSettingsView },
  { path: "/triage-settings", component: TriageSettingsView },
  { path: "/llm-settings", component: LlmSettingsView },
  { path: "/maintenance-settings", component: MaintenanceSettingsView },
  { path: "/troubleshooting-settings", component: TroubleshootingView },
];

const router = createRouter({
  history: createWebHashHistory(),
  routes,
});

const App = {
  components: { MailMessageItem, SummaryEntryItem },
  data() {
    return {
      query: "",
      chatLoading: false,
      currentSearchId: null,
      selectedSearchId: null,
      searchViewVersion: 0,
      activeSearchQuestion: "",
      pendingSearchClarification: null,
      clarificationResponse: "",
      searchCancelLoading: false,
      searchRetryAvailable: false,
      summaryRetryAvailable: false,
      searchHistory: [],
      searchHistoryOpen: false,
      searchHistoryLoading: false,
      searchHistoryRefreshPending: false,
      searchHistoryError: "",
      searchHistoryNextBeforeId: null,
      searchDeleteTarget: null,
      searchDeleteLoading: false,
      chatCards: [{ tone: "placeholder", meta: "No query yet.", body: "" }],
      messageDisplayMode: "comfort",
      statusSnapshot: null,
      statusError: "",
      statusLoading: false,
      statusPoller: null,
      statusFastPollsRemaining: 0,
      lastStatusSignature: "",
      retryingAccounts: {},
      llmPaused: false,
      llmPauseLoading: false,
      auth: authState,
      apiTokenInput: "",
    };
  },
  computed: {
    isSettingsRoute() {
      return this.$route.path !== "/";
    },
    statusPills() {
      if (!this.statusSnapshot) return [];
      const embeddings = this.statusSnapshot.embeddings || {};
      const claimableEmbeddings = Number(embeddings.claimable || 0);
      const waitingEmbeddings = Number(embeddings.waiting_for_analysis || 0);
      const embeddingDescription = claimableEmbeddings > 0
        ? `${claimableEmbeddings} ready now`
        : (waitingEmbeddings > 0 ? "waiting on analysis" : "caught up");
      return [
        { key: "analyses", label: "Analyses", ...this.statusSnapshot.analyses },
        { key: "embeddings", label: "Embeds", ...embeddings, description: embeddingDescription },
        { key: "clustering", label: "Clustering", ...this.statusSnapshot.clustering },
        { key: "summaries", label: "Summaries", ...this.statusSnapshot.summaries },
      ];
    },
    connectorStatuses() {
      const snapshot = this.statusSnapshot;
      if (!snapshot || !Array.isArray(snapshot.mailboxes)) return [];
      return snapshot.mailboxes.filter((mailbox) => mailbox.monitored && mailbox.account_status !== "disabled");
    },
    modelIndicators() {
      const snapshot = this.statusSnapshot;
      if (!snapshot || !Array.isArray(snapshot.models)) return [];
      return snapshot.models;
    },
  },
  mounted() {
    this.loadLlmPauseState();
    this.loadStatus();
  },
  unmounted() {
    this.clearStatusPoller();
  },
  methods: {
    clearStatusPoller() {
      if (this.statusPoller) clearTimeout(this.statusPoller);
      this.statusPoller = null;
    },
    submitApiToken() {
      const token = this.apiTokenInput.trim();
      if (!token) return;
      this.apiTokenInput = "";
      completeApiToken(token);
      this.loadLlmPauseState();
      this.loadStatus();
    },
    scheduleStatusPoll() {
      this.clearStatusPoller();
      const hasActiveModels = this.statusSnapshot && Array.isArray(this.statusSnapshot.models)
        ? this.statusSnapshot.models.some((model) =>
          Number(model.active_count || 0) > 0 ||
          Number(model.queued_count || 0) > 0
        )
        : false;
      const hasActiveJobs = this.statusPills.some((item) => Number(item.in_progress || 0) > 0);
      const delay = this.chatLoading ? 500 : ((this.statusFastPollsRemaining > 0 || hasActiveModels || hasActiveJobs) ? 1500 : 10000);
      this.statusPoller = setTimeout(() => this.loadStatus(), delay);
    },
    statusSignature(snapshot) {
      if (!snapshot) return "";
      const stages = ["analyses", "embeddings", "clustering", "summaries"].map((key) => {
        const item = snapshot[key] || {};
        return `${key}:${Number(item.done || 0)}/${Number(item.total || 0)}/${Number(item.in_progress || 0)}/${Number(item.retrying || 0)}/${Number(item.claimable || 0)}/${Number(item.waiting_for_analysis || 0)}/${item.status || ""}`;
      });
      const searchActivity = snapshot.search_activity || {};
      const searchStages = Object.entries(searchActivity.stages || {})
        .map(([stage, count]) => `${stage}:${Number(count || 0)}`)
        .sort();
      const modelParts = Array.isArray(snapshot.models)
        ? snapshot.models.map((model) => {
          return [
            model.preset_id,
            model.operation,
            model.state,
            Number(model.active_count || 0),
            Number(model.queued_count || 0),
            model.last_error_text || "",
          ].join(":");
        })
        : [];
      const mailboxParts = Array.isArray(snapshot.mailboxes)
        ? snapshot.mailboxes.map((mailbox) => `${mailbox.mailbox_id}:${Number(mailbox.total_messages || 0)}:${Number(mailbox.crawl_done || 0)}/${Number(mailbox.crawl_total || 0)}/${mailbox.crawl_status || ""}`)
        : [];
      return [...stages, ...searchStages, ...modelParts, ...mailboxParts].join("|");
    },
    getStatusColor(status) {
      if (status === "running") return "primary";
      if (status === "completed") return "success";
      if (status === "failed") return "error";
      return "grey";
    },
    statusTone(item) {
      if (!item || Number(item.total || 0) <= 0) return "idle";
      if (Number(item.in_progress || 0) > 0) return "running";
      return "done";
    },
    statusProgress(item) {
      if (!item) return 0;
      const total = Number(item.total || 0);
      if (total <= 0) return 0;
      return Math.max(0, Math.min(100, Math.round((Number(item.done || 0) / total) * 100)));
    },
    modelIndicatorTitle(model) {
      const parts = [
        model.preset_name || "Preset",
        `${model.operation || "generation"}: ${model.state || "healthy_idle"}`,
        `${Number(model.active_count || 0)} processing, ${Number(model.queued_count || 0)} queued`,
        `${model.provider || ""} · ${model.endpoint_url || ""}`,
        `priority ${model.priority ?? ""}`,
      ];
      if (model.model_names?.length) parts.push(model.model_names.join(", "));
      for (const stream of model.streams || []) {
        parts.push(stream.state === "receiving_output"
          ? `Receiving output · ${stream.progress_chunks} chunks · quiet ${stream.quiet_seconds}s`
          : `Waiting for first output · ${stream.elapsed_seconds}s`);
      }
      if (model.last_error_text) parts.push(model.last_error_text);
      if (model.cooldown_until) parts.push(`Recovery after ${new Date(model.cooldown_until).toLocaleString()}`);
      return parts.join(" | ");
    },
    modelIndicatorLabel(model) {
      const operation = model.operation === "embedding" ? "embeds" : "generation";
      return `${model.preset_name || "Preset"} · ${operation}`;
    },
    modelSlotTitle(slot) {
      const state = { active_search: "Search", active_pipeline: "Processing", failed_cooldown: "Unavailable", healthy_idle: "Idle" }[slot.state];
      return `Slot ${slot.number}: ${state}${slot.retiring ? " (finishing)" : ""}`;
    },
    connectorProgress(mailbox) {
      const total = Number(mailbox?.crawl_total || 0);
      if (total <= 0) return 100;
      return Math.max(0, Math.min(100, Math.round((Number(mailbox?.crawl_done || 0) / total) * 100)));
    },
    connectorSegmentSummary(mailbox) {
      return `crawl ${Number(mailbox?.crawl_done || 0)}/${Number(mailbox?.crawl_total || 0)}`;
    },
    mailboxItemCount(mailbox) {
      return Number(mailbox?.total_messages || 0);
    },
    connectorTone(mailbox) {
      const status = String(mailbox?.crawl_status || mailbox?.sync_status || "idle");
      if (status === "failed") return "failed";
      if (status === "running") return "running";
      if (status === "completed") return "done";
      return "idle";
    },
    async loadLlmPauseState() {
      this.llmPauseLoading = true;
      try {
        const data = await fetchJson("/api/llm-pause");
        this.llmPaused = Boolean(data.paused);
      } catch (error) {
        this.statusError = error.message;
      } finally {
        this.llmPauseLoading = false;
      }
    },
    async toggleLlmPause() {
      if (this.llmPauseLoading) return;
      this.llmPauseLoading = true;
      try {
        const data = await fetchJson("/api/llm-pause/toggle", { method: "POST" });
        this.llmPaused = Boolean(data.paused);
      } catch (error) {
        this.statusError = error.message;
      } finally {
        this.llmPauseLoading = false;
      }
    },
    markAccountSyncState(accountId, syncStatus) {
      if (!this.statusSnapshot || !Array.isArray(this.statusSnapshot.mailboxes)) return;
        this.statusSnapshot = {
          ...this.statusSnapshot,
          mailboxes: this.statusSnapshot.mailboxes.map((mailbox) =>
          Number(mailbox.account_id) === Number(accountId)
            ? { ...mailbox, sync_status: syncStatus, crawl_status: syncStatus }
            : mailbox
        ),
      };
    },
    async loadStatus() {
      this.statusLoading = true;
      try {
        const data = await fetchJson("/api/status");
        const nextSignature = this.statusSignature(data);
        if (this.lastStatusSignature && nextSignature !== this.lastStatusSignature) {
          this.statusFastPollsRemaining = Math.max(this.statusFastPollsRemaining, 4);
        }
        this.lastStatusSignature = nextSignature;
        this.statusSnapshot = data;
        this.updateChatProgressFromStatus(data);
        this.statusError = "";
      } catch (error) {
        this.statusError = error.message;
      } finally {
        if (this.statusFastPollsRemaining > 0) this.statusFastPollsRemaining -= 1;
        this.statusLoading = false;
        this.scheduleStatusPoll();
      }
    },
    updateChatProgressFromStatus(snapshot) {
      if (!this.chatLoading) return;
      if (this.currentSearchId) return;
      const activity = snapshot?.search_activity || {};
      const label = activity.label || (activity.active ? "Searching indexed mail" : "Waiting for search");
      this.chatCards = [{ tone: "placeholder", title: "Search in progress", meta: "Indexed mail only", body: label }];
    },
    isRetryingSync(accountId) {
      return Boolean(this.retryingAccounts[accountId]);
    },
    async retrySync(accountId) {
      if (!accountId || this.isRetryingSync(accountId)) return;
      this.retryingAccounts = { ...this.retryingAccounts, [accountId]: true };
      this.markAccountSyncState(accountId, "running");
      this.statusFastPollsRemaining = 8;
      try {
        await fetchJson(`/api/sync/${accountId}`, { method: "POST" });
        await this.loadStatus();
      } catch (error) {
        this.markAccountSyncState(accountId, "failed");
        this.statusError = error.message;
      } finally {
        this.retryingAccounts = { ...this.retryingAccounts, [accountId]: false };
      }
    },
    splitAnswerThoughts(answer, thoughtText) {
      const rawAnswer = String(answer || "").trim();
      let thoughts = String(thoughtText || "").trim();
      if (rawAnswer.includes("</think>")) {
        const [beforeClose, ...afterClose] = rawAnswer.split("</think>");
        thoughts = [thoughts, beforeClose.replace(/<think>/gi, "").trim()].filter(Boolean).join("\n\n");
        return { answer: afterClose.join("</think>").trim(), thoughtText: thoughts };
      }
      return { answer: rawAnswer, thoughtText: thoughts };
    },
    normalizeChatMessageSource(source) {
      return {
        id: `chat-message-${source.id}`,
        message_id: source.id,
        search_relevance: source.search_relevance || "",
        has_summary: Boolean(source.summary_text),
        subject: source.subject || source.title || "Untitled message",
        title: source.title || source.subject || "Untitled message",
        sender_name: source.sender_name || "",
        sender_address: source.sender_address || "",
        received_at: source.received_at || source.date || "",
        mailbox_name: source.mailbox || source.mailbox_name || "",
        summary_text: source.summary_text || "",
        preview_text: source.preview_text || source.snippet || "",
        snippet: source.snippet || "",
      };
    },
    async ask() {
      const query = this.query.trim();
      if (!query) return;
      this.pendingSearchClarification = null;
      this.clarificationResponse = "";
      await this.submitSmartSearch(query);
    },
    async submitSearchClarification() {
      const clarification = this.pendingSearchClarification;
      const response = this.clarificationResponse.trim();
      if (!clarification || !response || this.chatLoading) return;
      const query = [
        clarification.originalQuestion,
        "",
        `Clarification question: ${clarification.question}`,
        `Clarification response: ${response}`,
      ].join("\n");
      this.pendingSearchClarification = null;
      this.clarificationResponse = "";
      await this.submitSmartSearch(query);
    },
    async retrySmartSearch() {
      if (!this.activeSearchQuestion || this.chatLoading) return;
      if (this.summaryRetryAvailable && this.selectedSearchId) {
        const version = ++this.searchViewVersion;
        const searchId = this.selectedSearchId;
        this.chatLoading = true;
        this.currentSearchId = searchId;
        try {
          await fetchJson(`/api/search/${searchId}/retry-summary`, { method: "POST" });
          if (version === this.searchViewVersion) await this.pollSmartSearch(searchId);
        } catch (error) {
          if (version === this.searchViewVersion) this.chatCards = [
            { tone: "error", body: "Couldn’t restart the summary. Please try again." },
            ...this.chatCards.filter((card) => card.tone === "email-source"),
          ];
        } finally {
          if (version === this.searchViewVersion) {
            this.chatLoading = false;
            this.currentSearchId = null;
          }
        }
        return;
      }
      await this.submitSmartSearch(this.activeSearchQuestion);
    },
    openSearchHistory() {
      this.searchHistoryOpen = true;
      this.searchHistory = [];
      this.searchHistoryNextBeforeId = null;
      this.searchHistoryError = "";
      this.loadSearchHistory();
    },
    closeSearchHistory() {
      this.searchHistoryOpen = false;
    },
    async loadSearchHistory(append = false) {
      if (this.searchHistoryLoading) {
        if (!append) this.searchHistoryRefreshPending = true;
        return;
      }
      if (append && !this.searchHistoryNextBeforeId) return;
      this.searchHistoryLoading = true;
      try {
        const before = append ? `&before_id=${this.searchHistoryNextBeforeId}` : "";
        const data = await fetchJson(`/api/search?limit=50${before}`);
        const items = Array.isArray(data.searches) ? data.searches : [];
        this.searchHistory = append ? [...this.searchHistory, ...items] : items;
        this.searchHistoryNextBeforeId = data.next_before_id || null;
        this.searchHistoryError = "";
      } catch (error) {
        this.searchHistoryError = error.message;
      } finally {
        this.searchHistoryLoading = false;
        if (this.searchHistoryRefreshPending) {
          this.searchHistoryRefreshPending = false;
          this.loadSearchHistory();
        }
      }
    },
    searchHistoryDate(value) {
      return formatDate(value);
    },
    searchHistoryStatus(status) {
      return ({ queued: "Queued", running: "Running", succeeded: "Done", failed: "Failed", cancelled: "Stopped" })[status] || status;
    },
    async openSavedSearch(item) {
      this.closeSearchHistory();
      const searchId = Number(item.job_id);
      const version = ++this.searchViewVersion;
      this.selectedSearchId = searchId;
      this.activeSearchQuestion = item.question;
      this.pendingSearchClarification = null;
      this.clarificationResponse = "";
      this.searchRetryAvailable = false;
      this.chatLoading = false;
      this.currentSearchId = null;
      this.chatCards = [{ tone: "placeholder", title: "Opening saved search", body: "Loading saved result..." }];
      try {
        const data = await fetchJson(`/api/search/${searchId}`);
        if (version !== this.searchViewVersion) return;
        const search = data.search || {};
        if (["queued", "running"].includes(search.status)) {
          this.chatLoading = true;
          this.currentSearchId = searchId;
          await this.pollSmartSearch(searchId);
        } else {
          this.renderSmartSearchState(search);
        }
      } catch (error) {
        if (version === this.searchViewVersion) {
          this.chatCards = [{ tone: "error", title: "Could not open search", body: error.message }];
        }
      } finally {
        if (version === this.searchViewVersion) {
          this.chatLoading = false;
          this.currentSearchId = null;
        }
      }
    },
    closeSearch() {
      this.searchViewVersion += 1;
      this.selectedSearchId = null;
      this.chatLoading = false;
      this.currentSearchId = null;
      this.pendingSearchClarification = null;
      this.clarificationResponse = "";
      this.activeSearchQuestion = "";
      this.searchRetryAvailable = false;
      this.chatCards = [{ tone: "placeholder", meta: "Choose a saved search or ask a new question.", body: "" }];
    },
    startNewSearch() {
      this.closeSearch();
      this.query = "";
    },
    runSearchAgain(item) {
      if (!item?.question || ["queued", "running"].includes(item.status)) return;
      this.closeSearchHistory();
      this.submitSmartSearch(item.question);
    },
    requestRemoveSearch(item) {
      if (["queued", "running"].includes(item.status)) return;
      this.searchDeleteTarget = item;
    },
    closeSearchDeleteDialog(value) {
      if (!value && !this.searchDeleteLoading) this.searchDeleteTarget = null;
    },
    async confirmRemoveSearch() {
      const item = this.searchDeleteTarget;
      if (!item || this.searchDeleteLoading) return;
      this.searchDeleteLoading = true;
      try {
        await fetchJson(`/api/search/${item.job_id}`, { method: "DELETE" });
        if (Number(this.selectedSearchId) === Number(item.job_id)) this.closeSearch();
        this.searchHistory = this.searchHistory.filter((entry) => Number(entry.job_id) !== Number(item.job_id));
        this.searchDeleteTarget = null;
        if (this.searchHistoryOpen) await this.loadSearchHistory();
      } catch (error) {
        this.searchDeleteTarget = null;
        this.searchHistoryError = error.message;
      } finally {
        this.searchDeleteLoading = false;
      }
    },
    async submitSmartSearch(query) {
      this.summaryRetryAvailable = false;
      const version = ++this.searchViewVersion;
      this.pendingSearchClarification = null;
      this.clarificationResponse = "";
      this.query = "";
      this.activeSearchQuestion = query;
      this.selectedSearchId = null;
      this.chatLoading = true;
      this.currentSearchId = null;
      this.searchRetryAvailable = false;
      this.statusFastPollsRemaining = Math.max(this.statusFastPollsRemaining, 20);
      this.scheduleStatusPoll();
      this.chatCards = [{ tone: "placeholder", title: "Search in progress", meta: "Indexed mail only", body: "Starting search..." }];
      try {
        const idempotencyKey = globalThis.crypto?.randomUUID?.()
          || `search-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        const created = await fetchJson("/api/search", {
          method: "POST",
          body: JSON.stringify({ query, idempotency_key: idempotencyKey }),
        });
        if (version !== this.searchViewVersion) return;
        this.selectedSearchId = Number(created.search_id);
        this.currentSearchId = Number(created.search_id);
        await this.pollSmartSearch(this.currentSearchId);
      } catch (error) {
        if (version === this.searchViewVersion) {
          this.searchRetryAvailable = true;
          this.chatCards = [{ tone: "limitation", body: "Couldn’t finish updating this search. The emails already shown are still available." },
            ...this.chatCards.filter((card) => card.tone === "email-source")];
        }
      } finally {
        if (version === this.searchViewVersion) {
          this.chatLoading = false;
          this.currentSearchId = null;
          this.statusFastPollsRemaining = Math.max(this.statusFastPollsRemaining, 4);
          await this.loadStatus();
        }
      }
    },
    async pollSmartSearch(searchId) {
      const version = this.searchViewVersion;
      while (version === this.searchViewVersion && this.chatLoading && Number(this.currentSearchId) === Number(searchId) && Number(this.selectedSearchId) === Number(searchId)) {
        const data = await fetchJson(`/api/search/${searchId}`);
        if (version !== this.searchViewVersion || !this.chatLoading || Number(this.currentSearchId) !== Number(searchId) || Number(this.selectedSearchId) !== Number(searchId)) return;
        const search = data.search || {};
        const status = String(search.status || "");
        if (status === "queued" || status === "running") {
          const stopping = Boolean(search.cancel_requested);
          this.chatCards = [{
            tone: "placeholder",
            title: stopping ? "Stopping search" : "Search in progress",
            meta: this.smartSearchProgressMeta(search),
            body: stopping
              ? "Finishing the current operation, then stopping."
              : this.smartSearchProgressText(search),
          }, ...this.smartSearchMatchingCards(search)];
          await new Promise((resolve) => setTimeout(resolve, 750));
          continue;
        }
        this.renderSmartSearchState(search);
        return;
      }
    },
    renderSmartSearchState(search) {
      const status = String(search.status || "");
      this.summaryRetryAvailable = Boolean(search.can_retry_summary);
      this.searchRetryAvailable = status === "failed" && (Boolean(search.retryable) || this.summaryRetryAvailable);
      if (status === "succeeded") {
        this.renderSmartSearchResult(search);
      } else if (status === "cancelled") {
        this.chatCards = [{ tone: "placeholder", title: "Search stopped", body: "The emails already found are still available below." },
          ...this.smartSearchMatchingCards(search)];
      } else if (status === "expired") {
        this.chatCards = [{ tone: "error", title: "Search expired", body: "The retained result has expired. Run the search again." }];
      } else {
        this.chatCards = [{
          tone: "limitation",
          title: search.matching_sources?.length ? "Summary unavailable" : "Search unfinished",
          body: searchFailureMessage(search),
        }, ...this.smartSearchMatchingCards(search)];
      }
    },
    smartSearchSourceCard(source) {
      return { title: source.title,
        meta: [source.sender_name || source.sender_address, source.received_at ? formatDate(source.received_at) : "", source.mailbox].filter(Boolean).join(" | "),
        body: source.snippet || "No preview available.", tone: "email-source",
        message: this.normalizeChatMessageSource(source) };
    },
    smartSearchMatchingCards(search) {
      const sources = matchingSources(search);
      return sources.length ? [{ title: `Matching emails (${sources.length})`,
        body: "Search matches you can read now. They may not all support the answer." },
        ...sources.map((source) => ({ ...this.smartSearchSourceCard(source), showPreview: true }))] : [];
    },
    smartSearchRouteLabel(route) {
      return route?.embedding_model ? `Embedding model: ${route.embedding_model}` : "";
    },
    smartSearchProgressMeta(search) {
      const progress = search.progress || {};
      const completed = Number(progress.completed || 0);
      const total = Number(progress.total || 0);
      return total ? `${completed} of ${total} stages completed` : "Indexed mail only";
    },
    smartSearchProgressText(search) {
      const phase = search.phase || "queued";
      if (phase === "queued") return "Finding matching emails and preparing your summary.";
      const planning = search.stage_trace?.planning?.state === "running";
      const meaningSearch = search.stage_trace?.embedding?.state === "running";
      if (planning && meaningSearch) return "Understanding your question and preparing a search by meaning.";
      if (planning) return "Understanding your question.";
      if (meaningSearch) return "Preparing a search by meaning.";
      const compression = search.stage_trace?.compression || {};
      if (phase === "compression" && Number(compression.batches_total) > 0) {
        const completed = Number(compression.batches_completed || 0);
        const total = Number(compression.batches_total);
        return `Reviewing matching emails · ${completed} of ${total} groups completed.`;
      }
      const labels = {
        planning: "Understanding your question",
        embedding: "Preparing a search by meaning",
        preflight: "Checking indexed evidence",
        retrieval: "Retrieving relevant mail",
        materialization: "Preparing evidence",
        compression: "Reviewing matching emails",
        answering: "Writing the answer",
      };
      return `${labels[phase] || `Running ${phase}`}.`;
    },
    renderSmartSearchResult(search) {
      const result = search.result || {};
      if (result.state === "clarification") {
        const clarificationQuestion = result.clarification_question || "Please narrow the question.";
        this.pendingSearchClarification = {
          searchId: Number(search.search_id),
          originalQuestion: this.activeSearchQuestion,
          question: clarificationQuestion,
        };
        this.clarificationResponse = "";
        this.chatCards = [{
          title: "Clarification needed",
          meta: "Answer below to continue",
          body: clarificationQuestion,
        }, ...this.smartSearchMatchingCards(search)];
        return;
      }
      const limitations = [...new Set(
        (Array.isArray(result.limitations) ? result.limitations : [])
          .map((value) => String(value || "").trim())
          .filter(Boolean),
      )];
      this.chatCards = [
        {
          title: "Answer",
          meta: "Summary of retrieved emails",
          body: result.answer || "No indexed message supports that.",
        },
        ...(limitations.length ? [{
          title: "Search limits",
          body: limitations.join(" "),
          tone: "limitation",
        }] : []),
        ...((result.sources || []).some((source) => source.search_relevance === "supporting")
          ? [{ title: "Sources for this answer", body: "" }] : []),
        ...(result.sources || []).filter((source) => source.search_relevance === "supporting")
          .map((source) => this.smartSearchSourceCard(source)),
        ...this.smartSearchMatchingCards(search),
      ];
    },
    async stopSmartSearch() {
      if (!this.currentSearchId || this.searchCancelLoading) return;
      const searchId = this.currentSearchId;
      const version = this.searchViewVersion;
      this.searchCancelLoading = true;
      try {
        await fetchJson(`/api/search/${searchId}/cancel`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        if (version === this.searchViewVersion) {
          this.chatCards = [{
            tone: "placeholder",
            title: "Stopping search",
            body: "Finishing the current operation, then stopping.",
          }, ...this.chatCards.filter((card) => card.tone === "email-source")];
        }
      } catch (error) {
        if (version === this.searchViewVersion) this.chatCards = [{ tone: "error", body: "Couldn’t stop the search. Please try again." },
          ...this.chatCards.filter((card) => card.tone === "email-source")];
      } finally {
        this.searchCancelLoading = false;
      }
    },
  },
  template: `
    <v-app>
      <v-dialog :model-value="auth.required" persistent width="420">
        <v-card class="auth-dialog" variant="flat">
          <v-card-title>API Token</v-card-title>
          <v-card-text>
            <v-text-field
              v-model="apiTokenInput"
              label="Token"
              type="password"
              autofocus
              autocomplete="off"
              @keyup.enter="submitApiToken"
            />
          </v-card-text>
          <v-card-actions>
            <v-spacer />
            <v-btn color="primary" :disabled="!apiTokenInput.trim()" @click="submitApiToken">Continue</v-btn>
          </v-card-actions>
        </v-card>
      </v-dialog>
      <v-dialog :model-value="Boolean(searchDeleteTarget)" width="420" @update:model-value="closeSearchDeleteDialog">
        <v-card>
          <v-card-title>Remove saved search?</v-card-title>
          <v-card-text>This removes the saved answer and evidence for “{{ searchDeleteTarget?.question }}”.</v-card-text>
          <v-card-actions>
            <v-spacer />
            <v-btn variant="text" :disabled="searchDeleteLoading" @click="searchDeleteTarget = null">Cancel</v-btn>
            <v-btn color="error" :loading="searchDeleteLoading" @click="confirmRemoveSearch">Remove</v-btn>
          </v-card-actions>
        </v-card>
      </v-dialog>
      <v-dialog v-model="searchHistoryOpen" width="680" scrollable>
        <v-card>
          <v-card-title class="search-history-heading">
            <span>Search history</span>
            <span class="search-detail-actions">
              <v-btn icon="mdi-refresh" size="small" variant="text" title="Refresh history" aria-label="Refresh history" :loading="searchHistoryLoading" @click="loadSearchHistory()" />
              <v-btn icon="mdi-close" size="small" variant="text" title="Close history" aria-label="Close history" @click="closeSearchHistory" />
            </span>
          </v-card-title>
          <v-card-text class="search-history">
            <p v-if="searchHistoryError" class="search-history-error">{{ searchHistoryError }}</p>
            <div class="search-history-list">
              <div v-for="item in searchHistory" :key="item.job_id" class="search-history-item" :class="{ 'search-history-item--selected': Number(selectedSearchId) === Number(item.job_id) }">
                <button type="button" class="search-history-select" :title="item.question" @click="openSavedSearch(item)">
                  <span class="search-history-question">{{ item.question }}</span>
                  <span class="search-history-meta">{{ searchHistoryDate(item.created_at) }} · {{ searchHistoryStatus(item.status) }}</span>
                </button>
                <div class="search-history-actions">
                  <v-btn icon="mdi-replay" size="small" variant="text" title="Run again" aria-label="Run again" :disabled="['queued', 'running'].includes(item.status)" @click="runSearchAgain(item)" />
                  <v-btn icon="mdi-delete-outline" size="small" variant="text" title="Remove saved search" aria-label="Remove saved search" :disabled="['queued', 'running'].includes(item.status)" @click="requestRemoveSearch(item)" />
                </div>
              </div>
              <p v-if="!searchHistory.length && !searchHistoryLoading" class="subtle">No saved searches yet.</p>
            </div>
            <v-btn v-if="searchHistoryNextBeforeId" size="small" variant="text" :loading="searchHistoryLoading" @click="loadSearchHistory(true)">Older searches</v-btn>
          </v-card-text>
        </v-card>
      </v-dialog>
      <v-main>
        <div class="shell" :class="{ 'is-settings-open': isSettingsRoute }">
          <header class="hero">
            <div class="brand-block">
              <p class="eyebrow">Local Mail Intelligence</p>
              <h1>MailIntel</h1>
              <p class="lede">Search and triage for local mail.</p>
            </div>
            <div class="hero-actions">
              <v-btn-toggle v-model="messageDisplayMode" mandatory density="compact" variant="tonal" divided class="message-display-toggle" aria-label="Message display mode">
                <v-btn icon="mdi-format-list-bulleted" value="compact" size="small" title="Compact messages" aria-label="Compact messages" />
                <v-btn icon="mdi-card-text-outline" value="comfort" size="small" title="Comfort messages" aria-label="Comfort messages" />
                <v-btn icon="mdi-text-box-outline" value="full" size="small" title="Full messages" aria-label="Full messages" />
              </v-btn-toggle>
              <v-btn
                :icon="llmPaused ? 'mdi-play' : 'mdi-pause'"
                variant="tonal"
                size="small"
                :loading="llmPauseLoading"
                :aria-label="llmPaused ? 'Resume AI operations' : 'Pause AI operations'"
                :title="llmPaused ? 'Resume AI operations' : 'Pause AI operations and cancel ongoing requests'"
                @click="toggleLlmPause"
              />
              <v-menu location="bottom end">
                <template #activator="{ props }">
                  <v-btn v-bind="props" icon="mdi-cog-outline" variant="tonal" aria-label="Settings" />
                </template>
                <v-list density="compact">
                  <v-list-item prepend-icon="mdi-email-outline" title="Mail Accounts" to="/mail-settings" />
                  <v-list-item prepend-icon="mdi-tune-variant" title="Triage Settings" to="/triage-settings" />
                  <v-list-item prepend-icon="mdi-robot-outline" title="LLM Settings" to="/llm-settings" />
                  <v-list-item prepend-icon="mdi-tools" title="Maintenance" to="/maintenance-settings" />
                  <v-list-item prepend-icon="mdi-bug-outline" title="Troubleshooting" to="/troubleshooting-settings" />
                </v-list>
              </v-menu>
            </div>

            <v-card v-if="!isSettingsRoute" class="chat-panel" variant="flat">
              <h2>Search</h2>
              <form class="chat-form" @submit.prevent="ask">
                <v-text-field
                  v-model="query"
                  label="Search your mail"
                  placeholder="Ask about deadlines, collaborators, or topics"
                  density="compact"
                  hide-details
                  clearable
                />
                <div class="chat-form-actions">
                  <v-btn type="button" icon="mdi-history" variant="tonal" title="Search history" aria-label="Search history" @click="openSearchHistory" />
                  <v-btn type="submit" color="primary" prepend-icon="mdi-magnify" :disabled="!query.trim()">Search</v-btn>
                </div>
              </form>
              <section class="search-detail" aria-label="Selected search">
                  <div v-if="activeSearchQuestion" class="search-detail-heading">
                    <div class="search-detail-question">{{ activeSearchQuestion }}</div>
                    <div class="search-detail-actions">
                      <v-btn v-if="chatLoading" icon="mdi-stop" size="small" variant="tonal" color="error" title="Stop search" aria-label="Stop search" :disabled="!currentSearchId" :loading="searchCancelLoading" @click="stopSmartSearch" />
                      <v-btn v-if="searchRetryAvailable && !chatLoading" prepend-icon="mdi-refresh" size="small" variant="tonal" @click="retrySmartSearch">{{ summaryRetryAvailable ? 'Retry summary' : 'Retry search' }}</v-btn>
                      <v-btn icon="mdi-close" size="small" variant="text" :title="chatLoading ? 'Close view; search continues' : 'Close search view'" aria-label="Close search view" @click="closeSearch" />
                    </div>
                  </div>
                  <div :key="searchViewVersion" class="chat-answer" aria-live="polite">
                <template v-for="(card, index) in chatCards" :key="card.message ? 'mail-' + card.message.message_id : 'card-' + index">
                  <MailMessageItem
                    v-if="card.tone === 'email-source'"
                    :item="card.message"
                    :display-mode="messageDisplayMode"
                    :show-read-state="false"
                    :show-preview="Boolean(card.showPreview)"
                  />
                  <section v-else-if="card.summaryEntry" class="chat-result chat-result--citation">
                    <div v-if="card.title" class="chat-result-title">{{ card.title }}</div>
                    <div v-if="card.meta" class="chat-result-meta">{{ card.meta }}</div>
                    <SummaryEntryItem
                      :entry="card.summaryEntry"
                      :entry-key="'chat-summary-source-' + index"
                      :display-mode="messageDisplayMode"
                    />
                  </section>
                  <section v-else class="chat-result" :class="'chat-result--' + (card.tone || 'default')">
                    <div v-if="card.title" class="chat-result-title">{{ card.title }}</div>
                    <div v-if="card.meta" class="chat-result-meta">{{ card.meta }}</div>
                    <details v-if="card.thoughtText" class="chat-thoughts">
                      <summary>Show model thoughts</summary>
                      <div>{{ card.thoughtText }}</div>
                    </details>
                    <div class="chat-result-body">{{ card.body }}</div>
                  </section>
                </template>
                  </div>
                  <section v-if="pendingSearchClarification" class="chat-clarification" aria-label="Search clarification">
                <div class="chat-clarification-title">Follow-up needed</div>
                <div class="chat-clarification-question">{{ pendingSearchClarification.question }}</div>
                <form class="chat-clarification-form" @submit.prevent="submitSearchClarification">
                  <v-text-field
                    v-model="clarificationResponse"
                    label="Your answer"
                    placeholder="Add the missing detail"
                    density="compact"
                    hide-details
                    autofocus
                  />
                  <v-btn type="submit" color="primary" prepend-icon="mdi-arrow-right">Continue</v-btn>
                  <v-btn variant="text" prepend-icon="mdi-close" @click.prevent="startNewSearch">Start over</v-btn>
                </form>
                  </section>
              </section>
            </v-card>
          </header>
          <section class="status-strip" :class="{ 'status-strip--loading': statusLoading }" aria-live="polite">
            <div class="status-strip-summary">
              <span class="status-strip-label">Status</span>
              <span v-if="statusError" class="status-strip-error">{{ statusError }}</span>
              <span v-else-if="!connectorStatuses.length">No monitored mailboxes yet.</span>
              <div v-else class="status-connector-list">
                <div v-for="mailbox in connectorStatuses" :key="mailbox.mailbox_id" class="status-connector-card">
                  <div class="status-connector-row">
                    <div class="status-connector-meta">
                      <span class="status-connector-name">{{ mailbox.account_name }} / {{ mailbox.mailbox_name }}</span>
                      <v-chip size="x-small" variant="tonal" color="primary" class="count-chip status-count-chip">{{ mailboxItemCount(mailbox) }}</v-chip>
                      <v-chip size="x-small" :color="getStatusColor(mailbox.crawl_status || mailbox.sync_status)">crawl {{ mailbox.crawl_status || mailbox.sync_status || 'idle' }}</v-chip>
                      <v-btn
                        v-if="(mailbox.crawl_status || mailbox.sync_status) === 'failed'"
                        size="x-small"
                        variant="text"
                        icon="mdi-sync"
                        class="status-connector-retry"
                        :loading="isRetryingSync(mailbox.account_id)"
                        :disabled="isRetryingSync(mailbox.account_id)"
                        title="Retry sync"
                        @click="retrySync(mailbox.account_id)"
                      />
                    </div>
                    <span class="status-connector-count">{{ connectorSegmentSummary(mailbox) }}</span>
                  </div>
                  <div v-if="mailbox.crawl_last_error" class="status-connector-error">{{ mailbox.crawl_last_error }}</div>
                  <div class="status-connector-bar" :class="'status-connector-bar--' + connectorTone(mailbox)">
                    <span class="status-connector-fill" :style="{ width: connectorProgress(mailbox) + '%' }"></span>
                  </div>
                </div>
              </div>
            </div>
            <div class="status-strip-metrics">
              <div v-if="modelIndicators.length" class="model-indicator-strip">
                <span
                  v-for="model in modelIndicators"
                  :key="model.preset_id + ':' + model.operation"
                  class="model-indicator"
                  :title="modelIndicatorTitle(model)"
                >
                  <span class="model-indicator-slots">
                  <span v-for="slot in model.slots" :key="slot.number"
                    :class="'model-indicator--' + slot.state"
                    :title="modelSlotTitle(slot)">
                    <span class="model-indicator-light"></span>
                  </span>
                  </span>
                  <span class="model-indicator-label">{{ modelIndicatorLabel(model) }}</span>
                </span>
              </div>
              <div v-for="item in statusPills" :key="item.key" class="status-pill" :class="'status-pill--' + statusTone(item)">
                <div class="status-pill-head">
                  <span>{{ item.label }}</span>
                  <span>{{ item.description || (item.done + '/' + item.total) }}</span>
                </div>
                <div class="status-pill-bar">
                  <span
                    class="status-pill-fill"
                    :style="{ width: statusProgress(item) + '%' }"
                  ></span>
                </div>
              </div>
            </div>
          </section>
          <main class="view-stack">
            <router-view v-slot="{ Component }">
              <component :is="Component" :message-display-mode="messageDisplayMode" />
            </router-view>
          </main>
        </div>
      </v-main>
    </v-app>
  `,
};

const vuetify = createVuetify({
  components,
  directives,
  defaults: {
    VBtn: {
      rounded: "sm",
      elevation: 0,
    },
    VCard: {
      rounded: "sm",
      elevation: 0,
    },
    VTextField: {
      variant: "outlined",
      density: "compact",
    },
    VTextarea: {
      variant: "outlined",
      density: "compact",
    },
    VSelect: {
      variant: "outlined",
      density: "compact",
    },
    VSwitch: {
      inset: false,
    },
    VChip: {
      rounded: "sm",
    },
  },
  icons: {
    defaultSet: "mdi",
  },
  theme: {
    defaultTheme: "mailintel",
    themes: {
      mailintel: {
        dark: false,
        colors: {
          primary: "#2563eb",
          surface: "#ffffff",
          background: "#f7f8fa",
          error: "#b42318",
          success: "#16803c",
        },
      },
    },
  },
});

createApp(App)
  .component("SettingsPage", SettingsPage)
  .use(router)
  .use(vuetify)
  .mount("#app");

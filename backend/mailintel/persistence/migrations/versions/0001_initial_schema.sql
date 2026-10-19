-- Frozen initial schema, including indexes and default settings.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS cluster_rebuild_state (
	embedding_model TEXT NOT NULL,
	status TEXT DEFAULT 'idle' NOT NULL,
	dirty_since TIMESTAMP WITH TIME ZONE,
	lease_expires_at TIMESTAMP WITH TIME ZONE,
	attempt_count INTEGER DEFAULT 0 NOT NULL,
	last_error_text TEXT DEFAULT '' NOT NULL,
	last_completed_at TIMESTAMP WITH TIME ZONE,
	last_ready_embedding_count INTEGER DEFAULT 0 NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (embedding_model)
);

CREATE TABLE IF NOT EXISTS interest_profiles (
	id BIGSERIAL NOT NULL,
	name TEXT NOT NULL,
	description TEXT NOT NULL,
	collaborators_json JSONB DEFAULT '[]'::jsonb NOT NULL,
	signals TEXT DEFAULT '' NOT NULL,
	priority_weight FLOAT DEFAULT 1.0 NOT NULL,
	dashboard_prominence INTEGER DEFAULT 1 NOT NULL,
	summary_detail TEXT DEFAULT 'standard' NOT NULL,
	watch_mode TEXT DEFAULT 'watch' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS jobs (
	id BIGSERIAL NOT NULL,
	job_type TEXT NOT NULL,
	status TEXT DEFAULT 'running' NOT NULL,
	phase TEXT DEFAULT 'starting' NOT NULL,
	total_count INTEGER DEFAULT 0 NOT NULL,
	processed_count INTEGER DEFAULT 0 NOT NULL,
	current_item TEXT DEFAULT '' NOT NULL,
	error_text TEXT DEFAULT '' NOT NULL,
	metadata_json JSONB DEFAULT '{}'::jsonb NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS llm_presets (
	id BIGSERIAL NOT NULL,
	name TEXT NOT NULL,
	provider TEXT DEFAULT 'ollama' NOT NULL,
	enabled BOOLEAN DEFAULT false NOT NULL,
	priority INTEGER DEFAULT 100 NOT NULL,
	concurrency INTEGER DEFAULT 1 NOT NULL,
	capabilities_json JSONB DEFAULT '["mail_summary","mail_embedding","daily_summary","rollup_summary","topic_label","interval_embedding","search_plan","search_embedding","search_compress","search_answer"]'::jsonb NOT NULL,
	auth_mode TEXT DEFAULT 'env' NOT NULL,
	secret_locator TEXT DEFAULT '' NOT NULL,
	endpoint_url TEXT DEFAULT 'http://127.0.0.1:11434' NOT NULL,
	text_model TEXT DEFAULT 'llama3.1' NOT NULL,
	embedding_model TEXT DEFAULT 'nomic-embed-text' NOT NULL,
	temperature FLOAT DEFAULT 0.2 NOT NULL,
	text_context_window INTEGER DEFAULT 8192 NOT NULL,
	embedding_context_window INTEGER DEFAULT 8192 NOT NULL,
	max_output_tokens INTEGER DEFAULT 800 NOT NULL,
	initial_timeout_seconds FLOAT,
	streaming_timeout_seconds FLOAT,
	notes TEXT DEFAULT '' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CHECK (concurrency BETWEEN 1 AND 20),
	UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS mail_accounts (
	id BIGSERIAL NOT NULL,
	name TEXT NOT NULL,
	provider_type TEXT NOT NULL,
	auth_mode TEXT NOT NULL,
	secret_locator TEXT,
	encrypted_password TEXT,
	password_encryption TEXT,
	settings_json JSONB DEFAULT '{}'::jsonb NOT NULL,
	status TEXT DEFAULT 'active' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS topic_clusters (
	id BIGSERIAL NOT NULL,
	topic_key TEXT NOT NULL,
	embedding_model TEXT DEFAULT '' NOT NULL,
	name TEXT NOT NULL,
	description TEXT NOT NULL,
	key_terms_json JSONB DEFAULT '[]'::jsonb NOT NULL,
	key_entities_json JSONB DEFAULT '[]'::jsonb NOT NULL,
	llm_summary TEXT DEFAULT '' NOT NULL,
	why_it_matters TEXT DEFAULT '' NOT NULL,
	importance_score FLOAT DEFAULT 0.0 NOT NULL,
	model_name TEXT DEFAULT '' NOT NULL,
	centroid vector,
	dimensions INTEGER GENERATED ALWAYS AS (vector_dims(centroid)) STORED,
	frontier_at TIMESTAMP WITH TIME ZONE,
	algorithm_version TEXT DEFAULT '' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (topic_key)
);

CREATE TABLE IF NOT EXISTS user_profile (
	id SMALLINT NOT NULL,
	name TEXT DEFAULT '' NOT NULL,
	role TEXT DEFAULT '' NOT NULL,
	organization TEXT DEFAULT '' NOT NULL,
	aliases_json JSONB DEFAULT '[]'::jsonb NOT NULL,
	email_addresses_json JSONB DEFAULT '[]'::jsonb NOT NULL,
	collaborators_json JSONB DEFAULT '[]'::jsonb NOT NULL,
	communication_preferences TEXT DEFAULT '' NOT NULL,
	about TEXT DEFAULT '' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS llm_preset_stage_health (
	preset_id BIGINT NOT NULL,
	stage TEXT NOT NULL,
	status TEXT DEFAULT 'healthy' NOT NULL,
	consecutive_failures INTEGER DEFAULT 0 NOT NULL,
	last_failure_at TIMESTAMP WITH TIME ZONE,
	last_success_at TIMESTAMP WITH TIME ZONE,
	cooldown_until TIMESTAMP WITH TIME ZONE,
	last_error_text TEXT DEFAULT '' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (preset_id, stage),
	FOREIGN KEY(preset_id) REFERENCES llm_presets (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS mailboxes (
	id BIGSERIAL NOT NULL,
	account_id BIGINT NOT NULL,
	provider_mailbox_id TEXT NOT NULL,
	name TEXT NOT NULL,
	monitored BOOLEAN DEFAULT true NOT NULL,
	sync_cursor TEXT,
	last_synced_at TIMESTAMP WITH TIME ZONE,
	crawl_status TEXT DEFAULT 'idle' NOT NULL,
	crawl_lease_expires_at TIMESTAMP WITH TIME ZONE,
	crawl_attempt_count INTEGER DEFAULT 0 NOT NULL,
	crawl_last_error TEXT DEFAULT '' NOT NULL,
	next_sync_at TIMESTAMP WITH TIME ZONE,
	last_crawled_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	UNIQUE (account_id, provider_mailbox_id),
	FOREIGN KEY(account_id) REFERENCES mail_accounts (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pipeline_settings (
	id SMALLINT NOT NULL,
	analysis_mode TEXT DEFAULT 'priority' NOT NULL,
	embedding_mode TEXT DEFAULT 'priority' NOT NULL,
	canonical_embedding_model TEXT DEFAULT '' NOT NULL,
	search_planner_preset_id BIGINT,
	search_embedding_preset_id BIGINT,
	search_compressor_preset_id BIGINT,
	search_answer_preset_id BIGINT,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CHECK (id = 1),
	FOREIGN KEY(search_planner_preset_id) REFERENCES llm_presets (id) ON DELETE SET NULL,
	FOREIGN KEY(search_embedding_preset_id) REFERENCES llm_presets (id) ON DELETE SET NULL,
	FOREIGN KEY(search_compressor_preset_id) REFERENCES llm_presets (id) ON DELETE SET NULL,
	FOREIGN KEY(search_answer_preset_id) REFERENCES llm_presets (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS smart_search_jobs (
	job_id BIGINT NOT NULL,
	question TEXT NOT NULL,
	idempotency_key TEXT,
	status TEXT DEFAULT 'queued' NOT NULL,
	failed_stage TEXT,
	error_code TEXT,
	safe_error_text TEXT DEFAULT '' NOT NULL,
	retryable BOOLEAN DEFAULT false NOT NULL,
	cancel_requested BOOLEAN DEFAULT false NOT NULL,
	lease_expires_at TIMESTAMP WITH TIME ZONE,
	attempt_count INTEGER DEFAULT 0 NOT NULL,
	planner_preset_id BIGINT,
	embedding_preset_id BIGINT,
	compressor_preset_id BIGINT,
	answer_preset_id BIGINT,
	embedding_model TEXT DEFAULT '' NOT NULL,
	owner_timezone TEXT DEFAULT 'UTC' NOT NULL,
	embedding_dimensions INTEGER,
	preset_fingerprints_json JSONB DEFAULT '{}'::jsonb NOT NULL,
	work_json JSONB DEFAULT '{}'::jsonb NOT NULL,
	matching_sources_json JSONB DEFAULT '[]'::jsonb NOT NULL,
	rejected_message_ids_json JSONB DEFAULT '[]'::jsonb NOT NULL,
	plan_json JSONB DEFAULT '{}'::jsonb NOT NULL,
	stage_trace_json JSONB DEFAULT '{}'::jsonb NOT NULL,
	coverage_json JSONB DEFAULT '{}'::jsonb NOT NULL,
	result_json JSONB DEFAULT '{}'::jsonb NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (job_id),
	CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'expired')),
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE,
	UNIQUE (idempotency_key),
	FOREIGN KEY(planner_preset_id) REFERENCES llm_presets (id) ON DELETE SET NULL,
	FOREIGN KEY(embedding_preset_id) REFERENCES llm_presets (id) ON DELETE SET NULL,
	FOREIGN KEY(compressor_preset_id) REFERENCES llm_presets (id) ON DELETE SET NULL,
	FOREIGN KEY(answer_preset_id) REFERENCES llm_presets (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS mailbox_interval_summaries (
	id BIGSERIAL NOT NULL,
	mailbox_id BIGINT NOT NULL,
	summary_type TEXT NOT NULL,
	interval_start_date DATE NOT NULL,
	period_days INTEGER NOT NULL,
	refreshed_at TIMESTAMP WITH TIME ZONE,
	request_rebuild TIMESTAMP WITH TIME ZONE,
	summary_json JSONB DEFAULT '{"entries":[]}'::jsonb NOT NULL,
	search_document TSVECTOR DEFAULT ''::tsvector NOT NULL,
	embedding_status TEXT DEFAULT 'pending' NOT NULL,
	embedding_input_hash TEXT DEFAULT '' NOT NULL,
	embedding_model TEXT DEFAULT '' NOT NULL,
	embedding_lease_expires_at TIMESTAMP WITH TIME ZONE,
	embedding_attempt_count INTEGER DEFAULT 0 NOT NULL,
	embedding_last_error TEXT DEFAULT '' NOT NULL,
	embedding_needs_refresh BOOLEAN DEFAULT false NOT NULL,
	last_embedding_preset_name TEXT DEFAULT '' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (mailbox_id, interval_start_date, period_days),
	CHECK (period_days > 0),
	FOREIGN KEY(mailbox_id) REFERENCES mailboxes (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
	id BIGSERIAL NOT NULL,
	mailbox_id BIGINT NOT NULL,
	provider_message_id TEXT NOT NULL,
	internet_message_id TEXT,
	thread_id TEXT,
	subject TEXT NOT NULL,
	sender_name TEXT DEFAULT '' NOT NULL,
	sender_address TEXT DEFAULT '' NOT NULL,
	recipients_json JSONB DEFAULT '[]'::jsonb NOT NULL,
	cc_json JSONB DEFAULT '[]'::jsonb NOT NULL,
	received_at TIMESTAMP WITH TIME ZONE,
	sent_at TIMESTAMP WITH TIME ZONE,
	star INTEGER DEFAULT 0 NOT NULL,
	is_read BOOLEAN DEFAULT false NOT NULL,
	has_attachments BOOLEAN DEFAULT false NOT NULL,
	importance_hint TEXT,
	snippet_hash TEXT NOT NULL,
	metadata_json JSONB DEFAULT '{}'::jsonb NOT NULL,
	cleaned_text TEXT DEFAULT '' NOT NULL,
	search_document TSVECTOR DEFAULT ''::tsvector NOT NULL,
	fetched_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (mailbox_id, provider_message_id),
	FOREIGN KEY(mailbox_id) REFERENCES mailboxes (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS attachments (
	id BIGSERIAL NOT NULL,
	message_id BIGINT NOT NULL,
	provider_attachment_id TEXT,
	filename TEXT NOT NULL,
	mime_type TEXT NOT NULL,
	size_bytes INTEGER DEFAULT 0 NOT NULL,
	metadata_json JSONB DEFAULT '{}'::jsonb NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(message_id) REFERENCES messages (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS interval_summary_embeddings (
	summary_id BIGINT NOT NULL,
	preset_name TEXT NOT NULL,
	embedding_model TEXT NOT NULL,
	input_hash TEXT NOT NULL,
	dimensions INTEGER NOT NULL,
	status TEXT DEFAULT 'ready' NOT NULL,
	embedding vector,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (summary_id, embedding_model),
	FOREIGN KEY(summary_id) REFERENCES mailbox_interval_summaries (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message_body_cache (
	message_id BIGINT NOT NULL,
	body_text TEXT NOT NULL,
	source_segment TEXT DEFAULT 'body' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (message_id),
	FOREIGN KEY(message_id) REFERENCES messages (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message_clustering_work (
	message_id BIGINT NOT NULL,
	embedding_model TEXT NOT NULL,
	algorithm_version TEXT NOT NULL,
	source_hash TEXT NOT NULL,
	status TEXT DEFAULT 'todo' NOT NULL,
	lease_expires_at TIMESTAMP WITH TIME ZONE,
	retry_at TIMESTAMP WITH TIME ZONE,
	attempt_count INTEGER DEFAULT 0 NOT NULL,
	last_error_text TEXT DEFAULT '' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (message_id, embedding_model),
	CHECK (status IN ('todo', 'in_progress', 'done')),
	FOREIGN KEY(message_id) REFERENCES messages (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message_embeddings (
	message_id BIGINT NOT NULL,
	preset_name TEXT NOT NULL,
	embedding_model TEXT NOT NULL,
	input_hash TEXT NOT NULL,
	dimensions INTEGER NOT NULL,
	status TEXT DEFAULT 'ready' NOT NULL,
	embedding vector,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (message_id, embedding_model),
	FOREIGN KEY(message_id) REFERENCES messages (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message_interest_matches (
	message_id BIGINT NOT NULL,
	interest_profile_id BIGINT NOT NULL,
	score FLOAT NOT NULL,
	rationale TEXT NOT NULL,
	primary_match BOOLEAN DEFAULT false NOT NULL,
	assigned_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (message_id, interest_profile_id),
	FOREIGN KEY(message_id) REFERENCES messages (id) ON DELETE CASCADE,
	FOREIGN KEY(interest_profile_id) REFERENCES interest_profiles (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message_processing_state (
	message_id BIGINT NOT NULL,
	source_input_hash TEXT DEFAULT '' NOT NULL,
	interest_status TEXT DEFAULT 'pending' NOT NULL,
	interest_input_hash TEXT DEFAULT '' NOT NULL,
	summary_status TEXT DEFAULT 'pending' NOT NULL,
	summary_input_hash TEXT DEFAULT '' NOT NULL,
	embedding_status TEXT DEFAULT 'pending' NOT NULL,
	embedding_input_hash TEXT DEFAULT '' NOT NULL,
	embedding_model TEXT DEFAULT '' NOT NULL,
	interest_lease_expires_at TIMESTAMP WITH TIME ZONE,
	summary_lease_expires_at TIMESTAMP WITH TIME ZONE,
	embedding_lease_expires_at TIMESTAMP WITH TIME ZONE,
	interest_attempt_count INTEGER DEFAULT 0 NOT NULL,
	summary_attempt_count INTEGER DEFAULT 0 NOT NULL,
	embedding_attempt_count INTEGER DEFAULT 0 NOT NULL,
	interest_last_error TEXT DEFAULT '' NOT NULL,
	summary_last_error TEXT DEFAULT '' NOT NULL,
	embedding_last_error TEXT DEFAULT '' NOT NULL,
	summary_needs_refresh BOOLEAN DEFAULT false NOT NULL,
	embedding_needs_refresh BOOLEAN DEFAULT false NOT NULL,
	last_summary_preset_name TEXT DEFAULT '' NOT NULL,
	last_embedding_preset_name TEXT DEFAULT '' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (message_id),
	FOREIGN KEY(message_id) REFERENCES messages (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message_summaries (
	message_id BIGINT NOT NULL,
	summary_text TEXT NOT NULL,
	importance_score FLOAT NOT NULL,
	rationale TEXT NOT NULL,
	confidence FLOAT NOT NULL,
	model_name TEXT NOT NULL,
	summarized_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (message_id),
	FOREIGN KEY(message_id) REFERENCES messages (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS message_topic_memberships (
	message_id BIGINT NOT NULL,
	topic_cluster_id BIGINT NOT NULL,
	score FLOAT NOT NULL,
	is_primary BOOLEAN DEFAULT false NOT NULL,
	assigned_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (message_id, topic_cluster_id),
	FOREIGN KEY(message_id) REFERENCES messages (id) ON DELETE CASCADE,
	FOREIGN KEY(topic_cluster_id) REFERENCES topic_clusters (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS smart_search_job_messages (
	job_id BIGINT NOT NULL,
	message_id BIGINT NOT NULL,
	source_hash TEXT NOT NULL,
	PRIMARY KEY (job_id, message_id),
	FOREIGN KEY(job_id) REFERENCES smart_search_jobs (job_id) ON DELETE CASCADE,
	FOREIGN KEY(message_id) REFERENCES messages (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS work_queue (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          kind text NOT NULL, dedup_key text NOT NULL UNIQUE,
          payload jsonb NOT NULL DEFAULT '{}'::jsonb,
          priority integer NOT NULL DEFAULT 2,
          state text NOT NULL DEFAULT 'new' CHECK(state IN ('new','processing','paused')),
          revision bigint NOT NULL DEFAULT 1, claimed_revision bigint,
          retries integer NOT NULL DEFAULT 0, worker_id text,
          preset_id bigint REFERENCES llm_presets(id) ON DELETE SET NULL,
          target_preset_id bigint REFERENCES llm_presets(id) ON DELETE CASCADE,
          source_id bigint REFERENCES mail_accounts(id) ON DELETE CASCADE,
          created_at timestamptz NOT NULL DEFAULT now()
        );

CREATE INDEX IF NOT EXISTS work_queue_processing ON work_queue(id)
      WHERE state='processing';

CREATE INDEX IF NOT EXISTS work_queue_kind_new ON work_queue(kind, priority, id)
      WHERE state='new' AND target_preset_id IS NULL;

CREATE INDEX IF NOT EXISTS work_queue_target_new ON work_queue(target_preset_id, kind, priority, id)
      WHERE state='new' AND target_preset_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS work_queue_model_new ON work_queue
      (kind, (COALESCE(payload->>'embedding_model',payload->>'canonical_embedding_model','')), priority, id)
      WHERE state='new' AND target_preset_id IS NULL;

CREATE INDEX IF NOT EXISTS work_queue_target_model_new ON work_queue
      (target_preset_id, kind, (COALESCE(payload->>'embedding_model',payload->>'canonical_embedding_model','')), priority, id)
      WHERE state='new' AND target_preset_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS queue_workers (
          worker_id text PRIMARY KEY, state text NOT NULL,
          preset_id bigint REFERENCES llm_presets(id) ON DELETE CASCADE,
          source_id bigint REFERENCES mail_accounts(id) ON DELETE CASCADE,
          reason text NOT NULL DEFAULT '', changed_at timestamptz NOT NULL DEFAULT now()
        );

CREATE TABLE IF NOT EXISTS queue_events (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          occurred_at timestamptz NOT NULL DEFAULT now(), action text NOT NULL,
          job_id bigint, kind text, worker_id text, preset_id bigint, source_id bigint,
          retries integer, reason text NOT NULL DEFAULT ''
        );

CREATE INDEX IF NOT EXISTS work_queue_priority_id ON work_queue(priority,id);

CREATE INDEX IF NOT EXISTS work_queue_state_priority_id ON work_queue(state,priority,id);

CREATE INDEX IF NOT EXISTS idx_jobs_type_updated ON jobs (job_type, updated_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_topic_clusters_model_dimensions ON topic_clusters (embedding_model, dimensions);

CREATE INDEX IF NOT EXISTS idx_topic_clusters_frontier ON topic_clusters (embedding_model, frontier_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_smart_search_jobs_updated ON smart_search_jobs (updated_at DESC, job_id DESC);

CREATE INDEX IF NOT EXISTS idx_smart_search_jobs_claim ON smart_search_jobs (status, lease_expires_at, job_id);

CREATE INDEX IF NOT EXISTS idx_mailbox_interval_summaries_stale ON mailbox_interval_summaries (period_days, refreshed_at, interval_start_date);

CREATE INDEX IF NOT EXISTS idx_mailbox_interval_summaries_lookup ON mailbox_interval_summaries (mailbox_id, period_days, interval_start_date DESC);

CREATE INDEX IF NOT EXISTS idx_messages_received ON messages (received_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_mailbox ON messages (mailbox_id);

CREATE INDEX IF NOT EXISTS idx_interval_summary_embeddings_status ON interval_summary_embeddings (status);

CREATE INDEX IF NOT EXISTS idx_interval_summary_embeddings_model_dimensions_status ON interval_summary_embeddings (embedding_model, dimensions, status);

CREATE INDEX IF NOT EXISTS idx_message_clustering_work_claim ON message_clustering_work (embedding_model, status, retry_at, message_id);

CREATE INDEX IF NOT EXISTS idx_embeddings_model_dimensions_status ON message_embeddings (embedding_model, dimensions, status);

CREATE INDEX IF NOT EXISTS idx_embeddings_status ON message_embeddings (status);

CREATE INDEX IF NOT EXISTS idx_interest_matches_profile ON message_interest_matches (interest_profile_id, primary_match DESC, score DESC);

CREATE INDEX IF NOT EXISTS idx_topic_memberships_cluster ON message_topic_memberships (topic_cluster_id, is_primary DESC, score DESC);

CREATE INDEX IF NOT EXISTS idx_smart_search_job_messages_message ON smart_search_job_messages (message_id, job_id);

CREATE INDEX IF NOT EXISTS idx_messages_star ON messages (received_at DESC NULLS LAST, id DESC) WHERE star > 0;

CREATE INDEX IF NOT EXISTS idx_messages_search_document ON messages USING GIN(search_document);

CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw_1024_cosine ON message_embeddings USING hnsw ((embedding::vector(1024)) vector_cosine_ops) WHERE status = 'ready' AND dimensions = 1024;

CREATE INDEX IF NOT EXISTS idx_mailbox_interval_summaries_search_document ON mailbox_interval_summaries USING GIN(search_document);

CREATE INDEX IF NOT EXISTS idx_interval_summary_embeddings_hnsw_1024_cosine ON interval_summary_embeddings USING hnsw ((embedding::vector(1024)) vector_cosine_ops) WHERE status = 'ready' AND dimensions = 1024;

CREATE INDEX IF NOT EXISTS idx_mailbox_interval_summaries_request_rebuild ON mailbox_interval_summaries(request_rebuild) WHERE request_rebuild IS NOT NULL;

INSERT INTO pipeline_settings (id, analysis_mode, embedding_mode, canonical_embedding_model)
    VALUES (1, 'priority', 'priority', '')
    ON CONFLICT (id) DO NOTHING;

INSERT INTO llm_presets (name, provider, enabled, priority)
    SELECT 'Local Ollama', 'ollama', false, 100
    WHERE NOT EXISTS (SELECT 1 FROM llm_presets)
    ON CONFLICT (name) DO NOTHING;

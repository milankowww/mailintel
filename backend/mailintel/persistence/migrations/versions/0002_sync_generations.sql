-- SPDX-FileCopyrightText: Milan Pikula
-- SPDX-License-Identifier: EUPL-1.2

CREATE TABLE IF NOT EXISTS sync_counter (
    id smallint PRIMARY KEY CHECK (id = 1),
    generation bigint NOT NULL DEFAULT 0
);
INSERT INTO sync_counter(id, generation) VALUES (1, 0) ON CONFLICT DO NOTHING;

-- Serialize synchronized writes before taking their row locks. The counter's
-- transaction lock makes a visible generation also a safe committed watermark.
CREATE OR REPLACE FUNCTION sync_lock_writes() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM generation FROM sync_counter WHERE id = 1 FOR UPDATE;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION sync_assign_generation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    old_data jsonb;
    new_data jsonb;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.sync_generation <> -1 THEN
        SELECT jsonb_object_agg(key, value) INTO old_data
        FROM jsonb_each(to_jsonb(OLD)) WHERE key = ANY(string_to_array(TG_ARGV[0], ','));
        SELECT jsonb_object_agg(key, value) INTO new_data
        FROM jsonb_each(to_jsonb(NEW)) WHERE key = ANY(string_to_array(TG_ARGV[0], ','));
        IF old_data IS NOT DISTINCT FROM new_data THEN
            NEW.sync_generation := OLD.sync_generation;
            RETURN NEW;
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        UPDATE sync_counter SET generation = generation + 1 WHERE id = 1;
        RETURN OLD;
    END IF;
    UPDATE sync_counter SET generation = generation + 1 WHERE id = 1
        RETURNING generation INTO NEW.sync_generation;
    RETURN NEW;
END;
$$;

-- Relationships are sent as complete sets with their parent message.
CREATE OR REPLACE FUNCTION sync_touch_message() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD IS NOT DISTINCT FROM NEW THEN
        RETURN NULL;
    END IF;
    IF TG_OP <> 'INSERT' THEN
        UPDATE messages SET sync_generation = -1 WHERE id = OLD.message_id;
    END IF;
    IF TG_OP = 'INSERT' OR (TG_OP = 'UPDATE' AND NEW.message_id <> OLD.message_id) THEN
        UPDATE messages SET sync_generation = -1 WHERE id = NEW.message_id;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION sync_touch_account_mailboxes() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.name IS DISTINCT FROM NEW.name THEN
        UPDATE mailboxes SET sync_generation = -1 WHERE account_id = NEW.id;
    END IF;
    RETURN NULL;
END;
$$;
CREATE OR REPLACE TRIGGER sync_account_display AFTER UPDATE OF name ON mail_accounts
    FOR EACH ROW EXECUTE FUNCTION sync_touch_account_mailboxes();

DO $$
DECLARE
    item record;
    table_name text;
    key_column text;
    pending_count bigint;
    base_generation bigint;
BEGIN
    FOR item IN SELECT * FROM (VALUES
        ('messages', 'id,mailbox_id,provider_message_id,internet_message_id,thread_id,subject,sender_name,sender_address,recipients_json,cc_json,received_at,sent_at,star,is_read,has_attachments,importance_hint,cleaned_text'),
        ('message_summaries', 'message_id,summary_text,importance_score,rationale,confidence,model_name,summarized_at'),
        ('mailbox_interval_summaries', 'id,mailbox_id,summary_type,interval_start_date,period_days,refreshed_at,request_rebuild,summary_json'),
        ('mailboxes', 'id,account_id,provider_mailbox_id,name,monitored'),
        ('interest_profiles', 'id,name,description,collaborators_json,signals,priority_weight,dashboard_prominence,summary_detail,watch_mode'),
        ('topic_clusters', 'id,topic_key,embedding_model,name,description,key_terms_json,key_entities_json,llm_summary,why_it_matters,importance_score,model_name'),
        ('message_body_cache', 'message_id,body_text,source_segment'),
        ('smart_search_jobs', 'job_id,question,status,failed_stage,error_code,safe_error_text,retryable,matching_sources_json,coverage_json,result_json,created_at,completed_at')
    ) AS tables(name, fields)
    LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS sync_generation bigint NOT NULL DEFAULT 0', item.name);
        EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I(sync_generation)', 'idx_' || item.name || '_sync_generation', item.name);
        EXECUTE format('CREATE OR REPLACE TRIGGER sync_lock BEFORE INSERT OR UPDATE OR DELETE ON %I FOR EACH STATEMENT EXECUTE FUNCTION sync_lock_writes()', item.name);
        -- Reserve a range once, rather than updating the counter for every old
        -- record. ALTER TABLE above holds the table lock throughout this upgrade.
        EXECUTE format('SELECT count(*) FROM %I WHERE sync_generation = 0', item.name) INTO pending_count;
        IF pending_count > 0 THEN
            EXECUTE format('DROP TRIGGER IF EXISTS sync_generation ON %I', item.name);
            UPDATE sync_counter SET generation = generation + pending_count WHERE id = 1
                RETURNING generation - pending_count INTO base_generation;
            key_column := CASE item.name
                WHEN 'message_summaries' THEN 'message_id'
                WHEN 'message_body_cache' THEN 'message_id'
                WHEN 'smart_search_jobs' THEN 'job_id'
                ELSE 'id' END;
            EXECUTE format('WITH pending AS (
                SELECT %1$I AS key, row_number() OVER (ORDER BY %1$I) AS position
                FROM %2$I WHERE sync_generation = 0
            ) UPDATE %2$I AS target SET sync_generation = $1 + pending.position
                FROM pending WHERE target.%1$I = pending.key', key_column, item.name)
                USING base_generation;
        END IF;
        EXECUTE format('CREATE OR REPLACE TRIGGER sync_generation BEFORE INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION sync_assign_generation(%L)', item.name, item.fields);
    END LOOP;
    FOREACH table_name IN ARRAY ARRAY['message_interest_matches', 'message_topic_memberships'] LOOP
        EXECUTE format('CREATE OR REPLACE TRIGGER sync_lock BEFORE INSERT OR UPDATE OR DELETE ON %I FOR EACH STATEMENT EXECUTE FUNCTION sync_lock_writes()', table_name);
        EXECUTE format('CREATE OR REPLACE TRIGGER sync_parent AFTER INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION sync_touch_message()', table_name);
    END LOOP;
    -- Acquire the same lock before parent operations which can cascade into
    -- synchronized tables, avoiding reversed parent/counter lock ordering.
    FOREACH table_name IN ARRAY ARRAY['mail_accounts', 'jobs', 'llm_presets'] LOOP
        EXECUTE format('CREATE OR REPLACE TRIGGER sync_lock BEFORE INSERT OR UPDATE OR DELETE ON %I FOR EACH STATEMENT EXECUTE FUNCTION sync_lock_writes()', table_name);
    END LOOP;
END;
$$;

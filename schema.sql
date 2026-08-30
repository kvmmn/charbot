-- Chaharsotoon canonical Postgres schema.
-- Source of truth for identity / work / comms / ops.
-- No secrets. Apply tables first; compatibility views after dropping legacy public tables.

CREATE SCHEMA IF NOT EXISTS identity;
CREATE SCHEMA IF NOT EXISTS work;
CREATE SCHEMA IF NOT EXISTS comms;
CREATE SCHEMA IF NOT EXISTS ops;

-- ---------------------------------------------------------------------------
-- identity
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS identity.organizations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text UNIQUE NOT NULL,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identity.groups (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES identity.organizations(id) ON DELETE CASCADE,
    telegram_chat_id bigint UNIQUE,
    title text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identity.people (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES identity.organizations(id) ON DELETE CASCADE,
    slug text NOT NULL,
    display_name text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('board', 'staff', 'contractor', 'client')),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_people_org_kind
    ON identity.people (organization_id, kind);

CREATE TABLE IF NOT EXISTS identity.person_identities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id uuid NOT NULL REFERENCES identity.people(id) ON DELETE CASCADE,
    provider text NOT NULL CHECK (provider IN ('telegram', 'email', 'github')),
    provider_user_id text NOT NULL,
    username text,
    display_name text,
    is_primary boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_user_id),
    UNIQUE (person_id, provider)
);

CREATE TABLE IF NOT EXISTS identity.person_roles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id uuid NOT NULL REFERENCES identity.people(id) ON DELETE CASCADE,
    title text NOT NULL,
    department text,
    is_primary boolean NOT NULL DEFAULT false,
    source text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identity.person_memories (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id uuid NOT NULL REFERENCES identity.people(id) ON DELETE CASCADE,
    kind text NOT NULL,
    key text NOT NULL,
    value text NOT NULL,
    source text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (person_id, kind, key)
);

CREATE INDEX IF NOT EXISTS idx_person_memories_person_kind
    ON identity.person_memories (person_id, kind);

CREATE TABLE IF NOT EXISTS identity.person_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id uuid NOT NULL REFERENCES identity.people(id) ON DELETE CASCADE,
    event_type text NOT NULL,
    payload jsonb,
    telegram_message_id bigint,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- work
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS work.projects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES identity.organizations(id) ON DELETE CASCADE,
    slug text NOT NULL,
    name text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, slug)
);

CREATE TABLE IF NOT EXISTS work.tasks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES identity.organizations(id) ON DELETE CASCADE,
    group_id uuid REFERENCES identity.groups(id),
    project_id uuid REFERENCES work.projects(id),
    telegram_chat_id bigint NOT NULL,
    title text NOT NULL,
    description text,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'done', 'cancelled')),
    due_date date,
    created_by_person_id uuid REFERENCES identity.people(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_tasks_chat_status
    ON work.tasks (telegram_chat_id, status);

CREATE INDEX IF NOT EXISTS idx_tasks_due_date
    ON work.tasks (due_date);

CREATE TABLE IF NOT EXISTS work.task_assignees (
    task_id bigint NOT NULL REFERENCES work.tasks(id) ON DELETE CASCADE,
    person_id uuid NOT NULL REFERENCES identity.people(id),
    assigned_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, person_id)
);

CREATE INDEX IF NOT EXISTS idx_task_assignees_person
    ON work.task_assignees (person_id);

CREATE TABLE IF NOT EXISTS work.task_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id bigint NOT NULL REFERENCES work.tasks(id) ON DELETE CASCADE,
    event_type text NOT NULL,
    payload jsonb,
    actor_person_id uuid REFERENCES identity.people(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- comms
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS comms.messages (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id uuid REFERENCES identity.groups(id),
    person_id uuid REFERENCES identity.people(id),
    telegram_chat_id bigint NOT NULL,
    telegram_message_id bigint,
    telegram_update_id bigint UNIQUE,
    direction text NOT NULL CHECK (direction IN ('in', 'out')),
    kind text NOT NULL,
    body text,
    processed boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_created
    ON comms.messages (telegram_chat_id, created_at);

CREATE INDEX IF NOT EXISTS idx_messages_person
    ON comms.messages (person_id);

CREATE TABLE IF NOT EXISTS comms.message_media (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message_id bigint NOT NULL REFERENCES comms.messages(id) ON DELETE CASCADE,
    file_id text,
    storage_path text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS comms.lessons (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id uuid NOT NULL REFERENCES identity.organizations(id) ON DELETE CASCADE,
    body text NOT NULL,
    source_person_id uuid REFERENCES identity.people(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, body)
);

-- ---------------------------------------------------------------------------
-- ops
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ops.settings (
    organization_id uuid NOT NULL REFERENCES identity.organizations(id) ON DELETE CASCADE,
    group_id uuid REFERENCES identity.groups(id),
    key text NOT NULL,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_settings_org_group_key
    ON ops.settings (organization_id, group_id, key)
    WHERE group_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_settings_org_key_null_group
    ON ops.settings (organization_id, key)
    WHERE group_id IS NULL;

CREATE TABLE IF NOT EXISTS ops.schema_migrations (
    version integer PRIMARY KEY,
    name text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- COMPAT VIEWS
-- Apply only after legacy public tables are dropped (or on a fresh database).
-- search_path for the app is identity, work, comms, ops, public so unqualified
-- names hit real tables; leftover SQL that targets public.* uses these views.

CREATE OR REPLACE VIEW public.people AS
SELECT
    p.slug AS member_key,
    p.display_name,
    p.kind,
    CASE
        WHEN pi.provider_user_id ~ '^[0-9]+$' THEN pi.provider_user_id::bigint
        ELSE NULL
    END AS telegram_user_id,
    pi.username,
    p.created_at::text AS created_at,
    p.updated_at::text AS updated_at
FROM identity.people p
LEFT JOIN identity.person_identities pi
    ON pi.person_id = p.id
   AND pi.provider = 'telegram'
   AND pi.is_primary;

CREATE OR REPLACE VIEW public.tasks AS
SELECT
    t.id,
    t.telegram_chat_id AS group_id,
    t.title,
    t.description,
    (
        SELECT p.slug
        FROM work.task_assignees a
        JOIN identity.people p ON p.id = a.person_id
        WHERE a.task_id = t.id
        ORDER BY a.assigned_at ASC, p.slug ASC
        LIMIT 1
    ) AS assignee_key,
    t.due_date::text AS due_date,
    t.status,
    (
        SELECT CASE
            WHEN pi.provider_user_id ~ '^[0-9]+$' THEN pi.provider_user_id::bigint
            ELSE NULL
        END
        FROM identity.person_identities pi
        WHERE pi.person_id = t.created_by_person_id
          AND pi.provider = 'telegram'
        LIMIT 1
    ) AS created_by_user_id,
    t.created_at::text AS created_at,
    t.updated_at::text AS updated_at,
    t.completed_at::text AS completed_at
FROM work.tasks t;

CREATE OR REPLACE VIEW public.inbox AS
SELECT
    m.id,
    m.telegram_update_id,
    m.telegram_chat_id AS chat_id,
    NULL::text AS chat_type,
    g.title AS chat_title,
    CASE
        WHEN pi.provider_user_id ~ '^[0-9]+$' THEN pi.provider_user_id::bigint
        ELSE NULL
    END AS user_id,
    pi.username,
    COALESCE(pi.display_name, p.display_name) AS display_name,
    m.telegram_message_id AS message_id,
    m.kind,
    m.body AS text,
    (
        SELECT mm.file_id FROM comms.message_media mm
        WHERE mm.message_id = m.id
        ORDER BY mm.id ASC
        LIMIT 1
    ) AS file_id,
    (
        SELECT mm.storage_path FROM comms.message_media mm
        WHERE mm.message_id = m.id
        ORDER BY mm.id ASC
        LIMIT 1
    ) AS media_path,
    CASE WHEN m.processed THEN 1 ELSE 0 END AS processed,
    m.created_at::text AS created_at
FROM comms.messages m
LEFT JOIN identity.groups g ON g.id = m.group_id
LEFT JOIN identity.people p ON p.id = m.person_id
LEFT JOIN identity.person_identities pi
    ON pi.person_id = p.id
   AND pi.provider = 'telegram'
   AND pi.is_primary
WHERE m.direction = 'in';

CREATE OR REPLACE VIEW public.conversation AS
SELECT
    m.id,
    m.telegram_chat_id AS chat_id,
    m.direction,
    CASE
        WHEN pi.provider_user_id ~ '^[0-9]+$' THEN pi.provider_user_id::bigint
        ELSE NULL
    END AS user_id,
    pi.username,
    COALESCE(pi.display_name, p.display_name) AS display_name,
    m.kind,
    m.body AS text,
    (
        SELECT mm.storage_path FROM comms.message_media mm
        WHERE mm.message_id = m.id
        ORDER BY mm.id ASC
        LIMIT 1
    ) AS media_path,
    m.telegram_message_id,
    m.created_at::text AS created_at,
    p.slug AS member_key
FROM comms.messages m
LEFT JOIN identity.people p ON p.id = m.person_id
LEFT JOIN identity.person_identities pi
    ON pi.person_id = p.id
   AND pi.provider = 'telegram'
   AND pi.is_primary;

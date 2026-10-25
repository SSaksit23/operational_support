-- Group database (PostgreSQL 16 + pgvector). Safe to run again: everything is IF NOT EXISTS.
-- Personal IDs (citizen ID, passport) are stored encrypted, plus a keyed hash for matching
-- repeat customers. They never appear in chunks, embeddings or LLM prompts.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS groups (
    id            BIGSERIAL PRIMARY KEY,
    tour_code     TEXT UNIQUE NOT NULL,
    program_name  TEXT,
    program_code  TEXT,
    start_date    DATE,
    end_date      DATE,
    pax_total     INT,
    tour_leader   TEXT,
    company_id    TEXT,                -- billing company chosen for this group
    status        TEXT NOT NULL DEFAULT 'checked',  -- checked | approved | departed | closed
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS customers (
    id             BIGSERIAL PRIMARY KEY,
    passport_hash  TEXT UNIQUE,         -- HMAC of the passport number, for matching only
    passport_enc   BYTEA,               -- encrypted passport number
    citizen_id_enc BYTEA,               -- encrypted citizen ID
    given_name     TEXT NOT NULL,
    surname        TEXT NOT NULL,
    given_name_th  TEXT,
    surname_th     TEXT,
    gender         TEXT,
    dob            DATE,
    tel            TEXT,
    passport_expiry DATE,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bookings (
    group_id      BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    customer_id   BIGINT NOT NULL REFERENCES customers(id),
    room          TEXT,
    agency        TEXT,
    booking_no    TEXT,
    age           INT,
    remark        TEXT,
    is_tour_leader BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (group_id, customer_id)
);

CREATE TABLE IF NOT EXISTS suppliers (
    id        BIGSERIAL PRIMARY KEY,
    kind      TEXT NOT NULL,            -- hotel | land_operator | guide | airline
    name      TEXT NOT NULL,
    phone     TEXT,
    wechat    TEXT,
    address   TEXT,
    UNIQUE (kind, name)
);

CREATE TABLE IF NOT EXISTS group_suppliers (
    group_id    BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    supplier_id BIGINT NOT NULL REFERENCES suppliers(id),
    service_date DATE,
    detail      TEXT,                   -- e.g. "1 night", "optional tour 300 CNY"
    PRIMARY KEY (group_id, supplier_id, service_date)
);

CREATE TABLE IF NOT EXISTS documents (
    id          BIGSERIAL PRIMARY KEY,
    group_id    BIGINT REFERENCES groups(id) ON DELETE CASCADE,
    sha256      TEXT UNIQUE NOT NULL,   -- same file uploaded twice = one row
    doc_type    TEXT NOT NULL,
    file_name   TEXT NOT NULL,
    storage_uri TEXT,                   -- where the original file is kept (private storage)
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS facts (
    id          BIGSERIAL PRIMARY KEY,
    group_id    BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    document_id BIGINT REFERENCES documents(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,          -- flight | hotel | meeting | contact | optional_tour | cost ...
    value       JSONB NOT NULL,
    location    TEXT,                   -- page / cell / line in the document
    raw         TEXT
);

CREATE TABLE IF NOT EXISTS findings (
    id        BIGSERIAL PRIMARY KEY,
    group_id  BIGINT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    rule      TEXT NOT NULL,
    verdict   TEXT NOT NULL,
    title     TEXT NOT NULL,
    detail    TEXT,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id          BIGSERIAL PRIMARY KEY,
    group_id    BIGINT REFERENCES groups(id) ON DELETE CASCADE,
    document_id BIGINT REFERENCES documents(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,          -- page | person | flight | hotel | cost | finding ...
    location    TEXT,
    content     TEXT NOT NULL,          -- scrubbed of IDs before it is stored or embedded
    embedding   vector(%(dim)s) NOT NULL
);

CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_group_idx ON chunks (group_id);
CREATE INDEX IF NOT EXISTS facts_group_kind_idx ON facts (group_id, kind);
CREATE INDEX IF NOT EXISTS groups_dates_idx ON groups (start_date);

-- Freelance Auto-Bid Assistant — Multi-user data model
-- Consolidated from design discussion. See FUNCTIONALITY.md for the product
-- spec this maps to. Postgres-flavored (UUID, JSONB, TIMESTAMP).
--
-- Assumptions baked in here (flag if either is wrong):
--   1. account_type is a label only for now — NOT full multi-seat agency
--      support. An agency account is still one `users` row, one
--      `freelancer_profiles` row. Revisit if you need multiple people
--      operating under one shared account.
--   2. Clients who post jobs on Freelancer.com/Upwork/Fiverr are NOT rows
--      in `users` — they're external entities we only know about via the
--      platform's API/paste-in text. `projects` stores their info as plain
--      columns, not a FK into `users`.

-- ============================================================
-- 1. users — accounts on THIS product (freelancers using the tool)
-- ============================================================
CREATE TABLE users (
    id UUID PRIMARY KEY,
    name VARCHAR(255),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash TEXT,                  -- NULL if signed up via Google only
    auth_provider VARCHAR(20) DEFAULT 'EMAIL',  -- EMAIL, GOOGLE
    profile_image TEXT,
    account_type VARCHAR(20) NOT NULL DEFAULT 'SOLO_FREELANCER',
    -- SOLO_FREELANCER, AGENCY
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

-- ============================================================
-- 2. platform_connections — per-user OAuth link to each external platform
--    (the piece the original schema had nowhere to put)
-- ============================================================
CREATE TABLE platform_connections (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform VARCHAR(50) NOT NULL,          -- freelancer, upwork
    platform_user_id VARCHAR(100),          -- their numeric/string id on that platform
    platform_username VARCHAR(255),
    access_token_encrypted TEXT,
    refresh_token_encrypted TEXT,
    scope VARCHAR(255),

    -- per-platform reputation, since one user can have different ratings
    -- on Freelancer.com vs Upwork
    rating DECIMAL(2,1),
    total_reviews INT,

    connected_at TIMESTAMP,
    token_expires_at TIMESTAMP,
    last_synced_at TIMESTAMP,
    status VARCHAR(30) NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE, EXPIRED, REVOKED

    UNIQUE(user_id, platform)   -- enforces "one platform account per user" (FUNCTIONALITY.md §2.1)
);

-- ============================================================
-- 3. freelancer_profiles — the matching/drafting profile (1:1 with users)
-- ============================================================
CREATE TABLE freelancer_profiles (
    id UUID PRIMARY KEY,
    user_id UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    headline VARCHAR(255),
    bio TEXT,

    skills JSONB,           -- [{name, weight}, ...] — matches profile.py's weighted skills
    portfolio JSONB,
    experience JSONB,
    education JSONB,
    tone_samples JSONB,     -- writing-sample snippets used by proposal_writer.py

    -- rate RANGE, not a single value — matches the existing single-user
    -- prototype (profile.py), which already models min/max, not one rate
    rate_min DECIMAL(10,2),
    rate_max DECIMAL(10,2),
    currency VARCHAR(10),

    availability VARCHAR(30),        -- FULL_TIME, PART_TIME, NOT_AVAILABLE
    matching_criteria JSONB,         -- hard-reject keywords, budget floor, etc.

    -- Tokenized reference only — e.g. a Stripe customer/payment-method ID.
    -- Never store raw card/bank details here.
    payment_provider_customer_id VARCHAR(255),

    -- Different from platform_connections.last_synced_at (which tracks when
    -- we last pulled fresh project data from a given platform's API).
    -- This tracks when this freelancer's project board/recommendations
    -- were last recalculated — shown on the dashboard as "board updated Xm ago".
    last_synced_at TIMESTAMP,

    status VARCHAR(30) NOT NULL DEFAULT 'ACTIVE',   -- ACTIVE, INACTIVE
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

-- ============================================================
-- 4. projects — job postings ingested from external platforms
-- ============================================================
CREATE TABLE projects (
    id UUID PRIMARY KEY,

    platform VARCHAR(50) NOT NULL,          -- freelancer, upwork, fiverr
    external_id VARCHAR(100),               -- platform's own project/job id (NULL for pasted Fiverr text with no id)
    discovery_method VARCHAR(20) NOT NULL,  -- API_POLL, PASTE_IN

    -- Client info as sourced from the platform — NOT a FK, they're not our users
    client_name VARCHAR(255),
    client_rating DECIMAL(2,1),
    client_country VARCHAR(100),
    client_reviews_count INT,

    title VARCHAR(255),
    description TEXT,
    required_skills JSONB,

    work_type VARCHAR(20),      -- FIXED, HOURLY, MONTHLY

    currency VARCHAR(10),
    min_budget DECIMAL(12,2),
    max_budget DECIMAL(12,2),

    bid_information JSONB,      -- bid_count, avg_bid, etc. at time of fetch

    project_url TEXT,
    bid_deadline TIMESTAMP,          -- powers deadline alerts (§2.6/§2.7)
    proposal_expires_at TIMESTAMP,

    status VARCHAR(30) NOT NULL DEFAULT 'OPEN',   -- OPEN, CLOSED, AWARDED

    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),

    UNIQUE(platform, external_id)   -- idempotent upserts on repeated polling
);

-- ============================================================
-- 4b. skills / freelancer_skills / project_skills — normalized skill tags,
--     for fast SQL-level matching/sorting (the JSONB fields above stay as
--     the free-text source of truth; these are the queryable index of them)
-- ============================================================
CREATE TABLE skills (
    id UUID PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL        -- canonical name, e.g. "React"
);

CREATE TABLE freelancer_skills (
    freelancer_id UUID NOT NULL REFERENCES freelancer_profiles(id) ON DELETE CASCADE,
    skill_id UUID NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    weight DECIMAL(4,2) NOT NULL DEFAULT 1.0,
    tier VARCHAR(20) NOT NULL DEFAULT 'PRIMARY',   -- PRIMARY, SECONDARY
    PRIMARY KEY (freelancer_id, skill_id)
);

CREATE TABLE project_skills (
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    skill_id UUID NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, skill_id)
);

CREATE INDEX idx_freelancer_skills_skill ON freelancer_skills(skill_id);
CREATE INDEX idx_project_skills_skill ON project_skills(skill_id);

-- ============================================================
-- 5. recommendations — AI match scores, per freelancer per project
-- ============================================================
CREATE TABLE recommendations (
    id UUID PRIMARY KEY,

    freelancer_id UUID NOT NULL REFERENCES freelancer_profiles(id) ON DELETE CASCADE,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,

    score DECIMAL(5,2),
    recommendation_reason TEXT,     -- plain-language "why this score" (§2.5)

    is_hard_rejected BOOLEAN NOT NULL DEFAULT false,
    rejection_reason TEXT,          -- filled when is_hard_rejected — powers the
                                     -- "Rejected view" (§2.5) distinct from a user's
                                     -- own dismissal below

    status VARCHAR(30) NOT NULL DEFAULT 'NEW',
    -- NEW, VIEWED, APPLIED, DISMISSED
    -- (renamed from REJECTED to DISMISSED to avoid clashing with
    --  proposals.status REJECTED, which means the client said no — these
    --  are two different events at two different stages)

    recommended_at TIMESTAMP NOT NULL DEFAULT now(),

    UNIQUE(freelancer_id, project_id)
);

-- ============================================================
-- 6. proposals — drafted/submitted bids
-- ============================================================
CREATE TABLE proposals (
    id UUID PRIMARY KEY,

    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    freelancer_id UUID NOT NULL REFERENCES freelancer_profiles(id) ON DELETE CASCADE,
    recommendation_id UUID REFERENCES recommendations(id),

    proposal_text TEXT,
    bid_amount DECIMAL(12,2),
    estimated_days INT,
    milestones JSONB,      -- [{title, amount, description}, ...] for fixed-price (§2.5)

    submitted_via VARCHAR(20),   -- API, MANUAL_COPY — matches sanctioned-vs-not per platform

    status VARCHAR(30) NOT NULL DEFAULT 'DRAFT',
    -- DRAFT, SUBMITTED, ACCEPTED, REJECTED, WITHDRAWN

    submitted_at TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

-- ============================================================
-- 7. proposal_ai_versions — every AI-generated draft, for history/audit
-- ============================================================
CREATE TABLE proposal_ai_versions (
    id UUID PRIMARY KEY,
    proposal_id UUID NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    generated_prompt TEXT,
    generated_text TEXT,
    version INT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

-- ============================================================
-- 8. recommendation_feedback — signal for improving match quality
-- ============================================================
CREATE TABLE recommendation_feedback (
    id UUID PRIMARY KEY,
    recommendation_id UUID NOT NULL REFERENCES recommendations(id) ON DELETE CASCADE,
    feedback_type VARCHAR(30),   -- LIKE, DISLIKE, APPLIED
    comments TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

-- ============================================================
-- 9. notification_preferences — per-user channel opt-ins (§2.1, §2.7)
-- ============================================================
CREATE TABLE notification_preferences (
    id UUID PRIMARY KEY,
    user_id UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    email_enabled BOOLEAN NOT NULL DEFAULT true,   -- always-on baseline

    slack_enabled BOOLEAN NOT NULL DEFAULT false,
    slack_webhook_url TEXT,

    whatsapp_enabled BOOLEAN NOT NULL DEFAULT false,
    whatsapp_number VARCHAR(30),

    teams_enabled BOOLEAN NOT NULL DEFAULT false,
    teams_webhook_url TEXT,

    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

-- ============================================================
-- 10. subscriptions — billing (§2.9, Stripe)
-- ============================================================
CREATE TABLE subscriptions (
    id UUID PRIMARY KEY,
    user_id UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    stripe_customer_id VARCHAR(255),
    stripe_subscription_id VARCHAR(255),

    plan VARCHAR(50),                 -- e.g. BASIC, PRO
    status VARCHAR(30),               -- ACTIVE, PAST_DUE, CANCELED, TRIALING

    monthly_budget_limit DECIMAL(10,2),  -- user-set cap referenced by spend tracking (§2.6)

    current_period_start TIMESTAMP,
    current_period_end TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

-- ============================================================
-- Indexes worth adding beyond the implicit PK/UNIQUE ones
-- ============================================================
CREATE INDEX idx_recommendations_freelancer_status ON recommendations(freelancer_id, status);
CREATE INDEX idx_proposals_freelancer_status ON proposals(freelancer_id, status);
CREATE INDEX idx_projects_platform_status ON projects(platform, status);
CREATE INDEX idx_projects_bid_deadline ON projects(bid_deadline);

-- ============================================================
-- Relationships (summary)
-- ============================================================
-- users
--   ├── 1:1 freelancer_profiles
--   ├── 1:N platform_connections   (one per platform, unique per user+platform)
--   ├── 1:1 notification_preferences
--   └── 1:1 subscriptions
--
-- freelancer_profiles
--   ├── 1:N recommendations
--   ├── 1:N proposals
--   └── N:N skills (via freelancer_skills)
--
-- projects   (sourced from external platforms — no FK to users)
--   ├── 1:N recommendations
--   ├── 1:N proposals
--   └── N:N skills (via project_skills)
--
-- recommendations
--   ├── 0:1 proposal
--   └── 1:N recommendation_feedback
--
-- proposals
--   └── 1:N proposal_ai_versions

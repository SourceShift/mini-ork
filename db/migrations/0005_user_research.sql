-- mini-ork migration 0005 — user research (personas, JTBDs, journeys, steps)
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0005_user_research.sql
BEGIN;

-- ── personas ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS personas (
  id            TEXT PRIMARY KEY,                  -- e.g. "P001-dev"
  name          TEXT NOT NULL,
  description   TEXT NOT NULL,
  goals         TEXT,                              -- markdown bullet list
  pain_points   TEXT,
  source_doc    TEXT,                              -- e.g. "docs/PITCH.md#L42"
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at    TEXT,
  archived_at   TEXT
);

-- ── jtbds ─────────────────────────────────────────────────────────────────────
-- Jobs-To-Be-Done: atomic user goals that motivate feature development.
CREATE TABLE IF NOT EXISTS jtbds (
  id            TEXT PRIMARY KEY,                  -- e.g. "JTBD-001"
  persona_id    TEXT REFERENCES personas(id),
  statement     TEXT NOT NULL,                     -- "When I'm doing X, I want to Y, so I can Z"
  context       TEXT,
  outcome       TEXT,
  priority      TEXT CHECK (priority IN ('blocker','major','minor')) DEFAULT 'major',
  source_doc    TEXT,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  archived_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_jtbds_persona  ON jtbds(persona_id);
CREATE INDEX IF NOT EXISTS idx_jtbds_priority ON jtbds(priority) WHERE archived_at IS NULL;

-- ── journeys ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS journeys (
  id            TEXT PRIMARY KEY,                  -- e.g. "J007-compare-papers"
  jtbd_id       TEXT REFERENCES jtbds(id),
  title         TEXT NOT NULL,
  actor         TEXT,
  trigger       TEXT,
  goal          TEXT,
  steps_json    TEXT NOT NULL DEFAULT '[]',        -- array of step objects
  happy_path    TEXT,
  unhappy_paths TEXT,
  emotional_arc TEXT,
  source_doc    TEXT,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at    TEXT,
  archived_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_journeys_jtbd ON journeys(jtbd_id);

-- ── journey_steps ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS journey_steps (
  id            TEXT PRIMARY KEY,                  -- "J007-S03" = journey + step_no
  journey_id    TEXT NOT NULL REFERENCES journeys(id),
  step_no       INTEGER NOT NULL,
  route         TEXT,                              -- nullable — some steps are non-UI
  action        TEXT NOT NULL,
  expected_state TEXT,
  UNIQUE (journey_id, step_no)
);

CREATE INDEX IF NOT EXISTS idx_journey_steps_journey ON journey_steps(journey_id);
CREATE INDEX IF NOT EXISTS idx_journey_steps_route   ON journey_steps(route);

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0005_user_research.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'v1');

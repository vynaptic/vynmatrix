-- PostgreSQL Extensions
-- This script runs on first database initialization

-- Enable useful extensions
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- Trigram text similarity
CREATE EXTENSION IF NOT EXISTS btree_gin;     -- GIN index support
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";   -- UUID generation

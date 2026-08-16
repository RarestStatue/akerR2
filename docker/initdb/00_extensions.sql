CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gist;
-- No pgvector: the whole analytical surface fits in one model request (PLAN.md 1),
-- so there is nothing to embed. Adding a chatbot later = swap the image and
-- uncomment the line below.
-- CREATE EXTENSION IF NOT EXISTS vector;

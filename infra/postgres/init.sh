#!/bin/sh
# Creates the database roles and both databases. Runs once, on an empty data directory.
#   agentops_owner  owns the schema; used only for migrations and loading demo data
#   agentops_app    API and workers; not a table owner, so row-level security always applies
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" <<SQL
CREATE ROLE agentops_owner LOGIN PASSWORD '${AGENTOPS_OWNER_PASSWORD:-agentops_owner}';
CREATE ROLE agentops_app   LOGIN PASSWORD '${AGENTOPS_APP_PASSWORD:-agentops_app}';
CREATE DATABASE agentops      OWNER agentops_owner;
CREATE DATABASE agentops_test OWNER agentops_owner;
SQL
for db in agentops agentops_test; do
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$db" <<SQL
CREATE EXTENSION IF NOT EXISTS pg_trgm;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO agentops_app;
ALTER DEFAULT PRIVILEGES FOR ROLE agentops_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agentops_app;
ALTER DEFAULT PRIVILEGES FOR ROLE agentops_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO agentops_app;
SQL
done

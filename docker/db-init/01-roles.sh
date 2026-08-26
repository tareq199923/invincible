#!/bin/sh
# Least-privilege roles for the Invincible database.
#
#   invincible_migrate - owns the database and schema; the ONLY role that
#     may run DDL (`invincible db upgrade`). Non-superuser, no CREATEDB,
#     no CREATEROLE.
#   invincible_app     - runtime role: SELECT/INSERT/UPDATE/DELETE plus
#     sequence usage. No DDL, no superuser, no CREATEROLE/CREATEDB.
#
# The bootstrap superuser (`postgres`) exists only for this script; no
# application component ever connects as it. Passwords arrive via the
# environment - docker-compose.yml ships DEV-ONLY defaults that MUST be
# overridden for anything reachable outside localhost.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE invincible_migrate LOGIN PASSWORD '${INVINCIBLE_MIGRATE_PASSWORD}'
        NOSUPERUSER NOCREATEDB NOCREATEROLE;
    CREATE ROLE invincible_app LOGIN PASSWORD '${INVINCIBLE_APP_PASSWORD}'
        NOSUPERUSER NOCREATEDB NOCREATEROLE;

    ALTER DATABASE "$POSTGRES_DB" OWNER TO invincible_migrate;
    ALTER SCHEMA public OWNER TO invincible_migrate;

    GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO invincible_app;
    GRANT USAGE ON SCHEMA public TO invincible_app;

    -- Objects created by future migrations (run as invincible_migrate)
    -- become usable by the runtime role automatically.
    ALTER DEFAULT PRIVILEGES FOR ROLE invincible_migrate IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO invincible_app;
    ALTER DEFAULT PRIVILEGES FOR ROLE invincible_migrate IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO invincible_app;
EOSQL

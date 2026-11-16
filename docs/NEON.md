# Running LogLens on Neon Free PostgreSQL

Nothing here is provisioned automatically. This is the checklist for when you
decide to connect Neon.

## What the code already does for Neon

- Accepts Neon's `postgresql://...` URL (rewritten to the psycopg 3 driver).
- Detects the pooled endpoint (`-pooler` in the host) and disables server-side
  prepared statements, which PgBouncer's transaction mode does not support.
- Small connection pool, `pool_pre_ping`, and connection recycling every
  4 minutes, so connections closed by Neon's idle-suspend are replaced.
- Waits up to about a minute for the database at startup, covering the
  cold start after Neon suspends an idle compute.
- Idempotent startup migrations; no manual SQL needed.
- pgvector: the app already runs `CREATE EXTENSION IF NOT EXISTS vector`
  (Neon supports pgvector). Milestone 1 adds no new embeddings.

## Staying inside the free tier

Neon Free has a small storage allowance (about 0.5 GB per project at the time
of writing; **check Neon's current pricing page**, limits change). The app
defends against hitting it:

| Setting | Default | Effect |
|---|---|---|
| `DB_STORAGE_LIMIT_MB` | 400 | Uploads are refused (HTTP 507) above this database size. Keep it below Neon's limit. |
| `MAX_UPLOAD_MB` | 5 | Per-file cap. |
| `MAX_LINES_PER_UPLOAD` | 50000 | Per-file line cap. |
| `MAX_LOG_ENTRIES_PER_WORKSPACE` | 100000 | One workspace cannot use the whole budget. |
| `LOG_RETENTION_DAYS` | 14 | Workspace log rows older than this (by upload time) are deleted every 6 hours. Shared admin-uploaded logs and logs from before workspaces existed are kept. `0` disables. |

Raw files are not stored; only parsed rows are. Admins can check usage at
`GET /admin/storage` and purge immediately with `POST /admin/maintenance/purge`.
Note that Postgres reuses deleted space but the reported size only shrinks after
vacuum, which is why the cap sits below the real limit.

Do not enable paid plans, autoscaling above the free compute size, or extra
projects. Neon Free has no automatic upgrade, but check that no card is on file.

## Connecting (when you are ready)

1. Create a free account at neon.tech and a project (choose the region closest
   to your Render service). Do not add a payment method.
2. In the project's **Connect** dialog, choose the **pooled** connection string.
   It looks like
   `postgresql://USER:PASSWORD@ep-xxxx-pooler.REGION.aws.neon.tech/neondb?sslmode=require`.
3. Put it in Render: Service -> Environment -> add `DATABASE_URL` with that
   value. Render stores it as a secret; do not paste it into the repo, chat or
   a `.env` that is committed (`.env` is git-ignored).
4. Also set `SECRET_KEY`, and `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD`
   for the first admin. Leave `SIGNUP_ENABLED` unset (off).
5. Deploy. The first request after idle can take a few seconds while Neon wakes.
6. Verify: `GET /health` shows `"database": "postgres"`; `GET /admin/storage`
   (admin token) shows the size.

If a password is ever exposed, reset it in the Neon console and update
Render.

## Migrating existing data

The current demo database is separate. Starting fresh on Neon is simplest: the
app recreates tables and the bootstrap users on first start. Existing runbooks
would need re-uploading by an admin.

## Hosting note

Render's free web service sleeps when idle and has an ephemeral disk, which is
fine because uploads are not kept on disk. Do not use Render's own free
Postgres for persistent data; historically it expires after a limited period
(verify current terms).

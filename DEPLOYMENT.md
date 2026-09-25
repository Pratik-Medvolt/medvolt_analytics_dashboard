# Deployment Guide

This document covers running the Medvolt Analytics Django app in Docker,
both locally and on a production Ubuntu VM.

## Architecture

```
Internet
   |
   v
Nginx (container, port 80)   -- serves /static/ directly, proxies everything else
   |
   v
Gunicorn + Django (container, port 8000, internal only)
   |
   v
SQLite database file (Docker volume "sqlite_data")
```

There is no separate database container: the app uses SQLite, a
single file that lives on the `sqlite_data` Docker volume so it
survives container rebuilds/restarts. See "Database notes" below for
why this was kept as-is instead of switching to PostgreSQL.

## A. Environment variables

Copy the template and fill in real values:

```
cp .env.example .env
```

`.env` is git-ignored and must never be committed. Key variables:

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Django secret key. **Required** when `DEBUG=False` — the app refuses to start without it. Generate with `python3 -c 'import secrets; print(secrets.token_urlsafe(64))'`. |
| `DEBUG` | Must be `False` in production. |
| `ALLOWED_HOSTS` | Comma-separated hostnames the app answers to. Required when `DEBUG=False`. |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated scheme+host origins allowed to POST. Must be `https://` once `USE_HTTPS=True`; leave empty until then. |
| `USE_HTTPS` | **The single TLS switch.** Controls `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` and `SECURE_SSL_REDIRECT` together. Keep `False` until a certificate is actually in front of the app — see the warning below. |
| `SECURE_HSTS_SECONDS` | Opt-in HSTS, `0` by default. Set (e.g. `31536000`) only once HTTPS is proven working. |
| `NGINX_BIND` / `NGINX_PORT` | Where the Nginx container publishes. Defaults to `0.0.0.0:80`. Set to `127.0.0.1` / `8080` when a host proxy terminates TLS in front of it. |
| `DJANGO_DB_PATH` | SQLite file path. Docker Compose points this at the persistent volume; leave unset for local non-Docker runs. |
| `SQLITE_TIMEOUT` | Seconds a query waits for a competing writer before failing. Defaults to `20`. |

> **Do not set `USE_HTTPS=True` before TLS is working.** Browsers refuse
> to store a `Secure` cookie over `http://`, so the site loads, the health
> check passes, and nobody can get past the login form. The individual
> `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` / `SECURE_SSL_REDIRECT`
> variables still exist as per-setting overrides, but `USE_HTTPS` is what
> you should normally change. Settings will refuse to start on the one
> combination that is always wrong (Secure cookies plus an `http://`
> trusted origin).
| `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_SHEET_NAME` | Google service-account credentials/sheet used by the analytics collectors. |
| `GA4_PROPERTY_ID`, `GA4_LOOKBACK_DAYS`, `WEBSITE_BASE_URL` | GA4 collector config. |
| `SEARCH_CONSOLE_SITE_URL`, `SEARCH_CONSOLE_LOOKBACK_DAYS`, `SEARCH_CONSOLE_ROW_LIMIT` | Search Console collector config (`SEARCH_CONSOLE_END_LAG_DAYS` is obsolete and ignored). |
| `DASHBOARD_TIME_ZONE` | Timezone that defines "today" for the Search Console presets (default `Asia/Kolkata`); independent of Django's `TIME_ZONE`. |
| `WEBSITE_SITEMAP_URL`, `SUBSTACK_FEED_URL` | Content discovery config. |
| `MAILERLITE_API_KEY` | MailerLite collector config. |

**Google credentials file**: `google_credentials.json` is a secret and is
not baked into the Docker image or committed to git. Place the real file
next to `docker-compose.yml` on the host **before** running
`docker compose up` — it is bind-mounted read-only into the container.

The mount is declared with `create_host_path: false`, so a missing file
fails the start with a clear message naming the path. (Without that,
Docker silently creates a *directory* called `google_credentials.json`
and every Google collector then fails with a confusing read error that
persists until you delete it.)

The `web` container runs as a non-root user, so the file must be
readable by it. After copying it onto the host, run:

```
chmod 644 google_credentials.json
```

(If it stays at a restrictive mode like `600` owned by a different
user, the collector management commands will fail with a permission
error when they try to read it.)

## B. Building the image

```
docker compose build
```

## C. Starting the application (local Docker development)

```
docker compose up -d
```

This starts `web` (Gunicorn + Django) and `nginx` (reverse proxy on port
80). The `entrypoint.sh` script runs `migrate` and `collectstatic`
automatically every time the `web` container starts, before Gunicorn
launches.

Visit `http://localhost/` — you'll be redirected to the login page.

## D. Creating an admin user

```
docker compose exec web python manage.py createsuperuser
```

## E. Running migrations manually

Migrations run automatically on container start, but you can also run
them on demand (e.g. after pulling new migration files without
restarting):

```
docker compose exec web python manage.py migrate
```

## F. Collecting static files manually

Also automatic on start, but can be re-run if needed:

```
docker compose exec web python manage.py collectstatic --noinput
```

## G. Viewing logs

```
docker compose logs -f web
docker compose logs -f nginx
```

## H. Restarting containers

```
docker compose restart web
docker compose restart nginx
```

## I. Updating the application after a `git pull`

```
git pull
docker compose build
docker compose up -d
```

Migrations and `collectstatic` run automatically as part of container
startup, so no extra manual steps are required for normal code changes.

## J. Running the periodic analytics collectors

The management commands (`collect_all`, `collect_ga4`,
`collect_mailerlite`, `collect_search_console`, `discover_content`,
`generate_weekly_report`, plus the read-only `verify_ga4` / `verify_gsc`
checks) are not part of the web request cycle in this
app today — they're one-off/scheduled jobs. Keep that as-is (no Celery
or Redis was introduced) and schedule them with the host's crontab,
running them *inside* the running `web` container:

Cron runs with a minimal `PATH` and no shell profile, so use the
absolute path to `docker` and an explicit `--project-directory` rather
than relying on `cd` plus a bare `docker compose`:

```
# crontab -e on the VM
30 5 * * * /usr/bin/docker compose --project-directory /opt/medvolt exec -T web python manage.py collect_all >> /var/log/medvolt-collect.log 2>&1
```

The command exits non-zero if any individual collector failed, while
still committing the data the other collectors returned, so a non-zero
exit in the log means "check this one", not "nothing ran".

## K. Basic production deployment on a fresh Ubuntu VM

```bash
# 1. Install Docker Engine + Compose plugin
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# log out/in for the group change to take effect

# 2. Get the code
git clone <repository-url> medvolt
cd medvolt

# 3. Configure environment
cp .env.example .env
nano .env   # fill in SECRET_KEY, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, API keys...

# 4. Place the Google service-account credentials file
# (copy google_credentials.json onto the VM via scp — do not commit it)
scp google_credentials.json user@vm:/opt/medvolt/google_credentials.json
ssh user@vm chmod 644 /opt/medvolt/google_credentials.json

# 5. Build and start
docker compose build
docker compose up -d

# 6. Create an admin user
docker compose exec web python manage.py createsuperuser
```

At this point the app is reachable over plain HTTP on port 80, and you
should be able to log in. Verify that before adding TLS — debugging one
layer at a time is much faster than two.

Security group on the instance: SSH from your address only, plus 80 and
443. Gunicorn stays on the Docker network and is never published.

## K2. Routing and TLS (Traefik)

This stack publishes **no host port**. The instance already runs Traefik
v3.6, which owns `:80` and `:443` and terminates TLS with its own
Let's Encrypt resolver. Traefik reaches this stack over the shared
external Docker network `web`, exactly as the Dify and n8n stacks do.

Only the `nginx` container joins `web`; Gunicorn stays on the project's
private network and is never reachable from outside the stack.

Routing is declared with labels on the `nginx` service and driven by
`DASHBOARD_HOST` in `.env`:

| Label | Value |
|---|---|
| `traefik.enable` | `true` (Traefik runs with `exposedbydefault=false`) |
| `traefik.docker.network` | `web` |
| `traefik.http.routers.medvolt.entrypoints` | `websecure` |
| `traefik.http.routers.medvolt.rule` | ``Host(`${DASHBOARD_HOST}`)`` |
| `traefik.http.routers.medvolt.tls.certresolver` | `letsencrypt` |
| `traefik.http.services.medvolt.loadbalancer.server.port` | `80` |

`DASHBOARD_HOST` is required — `docker compose up` refuses to start
without it rather than registering a router with an empty rule.

**Before the first start**, the DNS A record for `DASHBOARD_HOST` must
already point at this instance. Traefik uses the HTTP-01 challenge, so
without working DNS no certificate is issued and the route serves a
TLS error.

Because Traefik terminates TLS, `.env` should carry `USE_HTTPS=True` and
an `https://` `CSRF_TRUSTED_ORIGINS`. Traefik forwards
`X-Forwarded-Proto: https`, and this stack's Nginx preserves that value
rather than overwriting it with its own scheme — without that, Django
would see an insecure request and `SECURE_SSL_REDIRECT` would produce an
infinite redirect.

The container health check sends a `Host` header taken from
`DASHBOARD_HOST` (falling back to the first entry of `ALLOWED_HOSTS`).
It connects on loopback, so a bare request would arrive as
`Host: 127.0.0.1` and be rejected by `ALLOWED_HOSTS`, leaving `web`
permanently unhealthy and stopping Nginx from starting.

To verify routing without a browser, from any container on the `web`
network:

```bash
docker run --rm --network web curlimages/curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Host: analytics.medvolt.ai" -H "X-Forwarded-Proto: https" \
  http://medvolt-nginx-1/login/
```

## K3. Surviving a reboot

`restart: unless-stopped` only helps if the Docker daemon itself starts
at boot:

```bash
sudo systemctl enable --now docker
sudo reboot   # then re-check the dashboard
```

## L. Backup considerations

The database is a single SQLite file inside the `sqlite_data` Docker
volume. Back it up regularly, e.g. via a host cron job:

Use the `backup_db` management command. It takes a consistent snapshot
through SQLite's online backup API (a plain file copy of a database that
is being written to can be corrupt) and prunes old snapshots. It needs no
`sqlite3` CLI, which the slim base image does not ship — the previously
documented `sqlite3 .backup` cron line could never have run.

```bash
# crontab -e on the VM
0 3 * * * /usr/bin/docker compose --project-directory /opt/medvolt exec -T web python manage.py backup_db --retain-days 14 >> /var/log/medvolt-backup.log 2>&1
```

Snapshots land in `/app/data/backups/` inside the `sqlite_data` volume.
Verify one by hand after the first run:

```bash
docker compose exec web python manage.py backup_db
docker compose exec web ls -la /app/data/backups/
```

To restore: stop the `web` container, copy the chosen snapshot over
`/app/data/db.sqlite3` inside the volume, and start it again. Rehearse
this once on the fresh install, while there is nothing to lose.

Also back up:
- `google_credentials.json` (kept outside git; store it in your secrets manager).
- The `.env` file (contains API keys and the Django secret key).

Copy backups off the VM (e.g. to S3 or another host) — a volume backup
that lives on the same disk as the running container does not protect
against disk failure.

## Database notes: why SQLite was kept

The app already uses SQLite with no existing PostgreSQL configuration,
and per the containerization goals, we did not migrate it automatically.

**SQLite is a reasonable choice here for a small, low-concurrency
internal dashboard** (few dashboard viewers, and writes happening only
from scheduled collector jobs, not concurrent web requests). Trade-offs
to be aware of if load grows:

- SQLite serializes writes; concurrent writes from multiple processes
  can raise "database is locked" errors. Keep the `web` service at a
  single replica (already the default) and avoid running the collector
  management commands at the same moment the dashboard is under heavy
  write load (it currently isn't — the dashboard is read-only).
- No built-in replication or point-in-time recovery — backups are your
  only recovery path, so the cron backup above matters.
- If you outgrow this (multiple app replicas, high write concurrency,
  need for managed backups/replication), migrate to PostgreSQL. That
  would mean: add a `db` service to `docker-compose.yml`, add
  `psycopg2-binary` to `requirements.txt`, and change `DATABASES` in
  `settings.py` to point at it — plus a one-time data migration. This
  was intentionally not done now since the current scale doesn't need
  it and it would be an unnecessary risk to introduce during a
  containerization pass.

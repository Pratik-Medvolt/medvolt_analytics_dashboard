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
| `SECRET_KEY` | Django secret key. Generate a long random value for production. |
| `DEBUG` | Must be `False` in production. |
| `ALLOWED_HOSTS` | Comma-separated list of hostnames the app will answer to. |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated list of scheme+host origins allowed to POST (e.g. `https://your-domain.com`). |
| `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` | Keep `True` once served over HTTPS. |
| `SECURE_SSL_REDIRECT` | Set `True` only after HTTPS is actually configured in front of Nginx. |
| `DJANGO_DB_PATH` | SQLite file path. Docker Compose points this at the persistent volume; leave unset for local non-Docker runs. |
| `GOOGLE_CREDENTIALS_FILE`, `GOOGLE_SHEET_NAME` | Google service-account credentials/sheet used by the analytics collectors. |
| `GA4_PROPERTY_ID`, `GA4_LOOKBACK_DAYS`, `WEBSITE_BASE_URL` | GA4 collector config. |
| `SEARCH_CONSOLE_SITE_URL`, `SEARCH_CONSOLE_LOOKBACK_DAYS`, `SEARCH_CONSOLE_END_LAG_DAYS`, `SEARCH_CONSOLE_ROW_LIMIT` | Search Console collector config. |
| `WEBSITE_SITEMAP_URL`, `SUBSTACK_FEED_URL` | Content discovery config. |
| `MAILERLITE_API_KEY` | MailerLite collector config. |

**Google credentials file**: `google_credentials.json` is a secret and is
not baked into the Docker image or committed to git. Place the real file
next to `docker-compose.yml` on the host before running `docker compose up`
— it is bind-mounted read-only into the container.

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
`generate_weekly_report`) are not part of the web request cycle in this
app today — they're one-off/scheduled jobs. Keep that as-is (no Celery
or Redis was introduced) and schedule them with the host's crontab,
running them *inside* the running `web` container:

```
# crontab -e on the VM
0 6 * * * cd /opt/medvolt && docker compose exec -T web python manage.py collect_all >> /var/log/medvolt-collect.log 2>&1
```

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

At this point the app is reachable on port 80. For a real domain with
HTTPS, either:
- Put a TLS-terminating reverse proxy (e.g. Caddy, or Nginx run
  directly on the host with certbot) in front of the `nginx` container
  and change its port mapping to a non-public port, or
- Add a certbot/Let's Encrypt setup to the `nginx` service.

Either approach is a small addition on top of this setup; it wasn't
included by default to keep the base configuration minimal and because
domain/TLS decisions are environment-specific.

## L. Backup considerations

The database is a single SQLite file inside the `sqlite_data` Docker
volume. Back it up regularly, e.g. via a host cron job:

```bash
# crontab -e on the VM
0 3 * * * docker compose -f /opt/medvolt/docker-compose.yml exec -T web sh -c \
  "sqlite3 /app/data/db.sqlite3 '.backup /app/data/backup-$(date +\%F).sqlite3'"
```

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

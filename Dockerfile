# MateBot AI (MMAI) web service for DigitalOcean App Platform (T17). One container runs the FastAPI app, the T8 scheduler and
# the AI queue, so the app must run as a SINGLE instance (set in .do/app.yaml). Data lives in the managed PostgreSQL database
# given by DATABASE_URL; nothing is written to the container disk except the local SQLite file when DATABASE_URL is unset.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PORT=8080
WORKDIR /app

# Matching major-version clients are required for a PostgreSQL 18 recovery drill.
# Official signed repository: https://www.postgresql.org/download/linux/debian/
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl --fail --silent --show-error --location \
       https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && gpg --show-keys --with-colons /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
       | grep -q '^fpr:::::::::B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8:' \
    && . /etc/os-release \
    && printf 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt %s-pgdg main\n' "$VERSION_CODENAME" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && pg_dump --version && pg_restore --version \
    && rm -rf /var/lib/apt/lists/*

COPY mmai-app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mmai-app/server ./server
COPY mmai-app/client ./client
COPY mmai-app/data ./data
COPY mmai-app/tools ./tools
COPY homebot-app /homebot-app
COPY mmai-app/start.sh mmai-app/publish.env mmai-app/README.md mmai-app/FLOWS.md ./
RUN chmod +x start.sh && useradd -r -u 10001 matebot && mkdir -p server/data && chown -R matebot /app
USER matebot

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python3 -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/api/health', timeout=4).status == 200 else 1)"
CMD ["./start.sh"]

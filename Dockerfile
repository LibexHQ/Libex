FROM python:3.12-slim@sha256:a39549e211a16149edf74e5fdc9ef03a6767e46cd987c5048b6659b6c9904c94

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade "pip>=26.1.2"

COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY . .

# The interactive docs are served from assets Libex ships rather than from a
# CDN, so a visitor's IP address never reaches a third party to render them.
# Pinned versions, checksum-verified -- a substituted file fails the build
# instead of being served to a browser.
RUN sh scripts/fetch_docs_assets.sh

RUN mkdir -p /app/logs \
    && useradd -m -u 1000 libex \
    && chown -R libex:libex /app
USER libex

EXPOSE 3333

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3333"]
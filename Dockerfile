# Build the stylesheet with the standalone Tailwind CLI, so the image needs no Node.
FROM debian:bookworm-slim AS css
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Pinned and verified. "latest" means the image silently changes what it builds, and a
# stylesheet that differs from the one CI compared against is a UI nobody reviewed.
ARG TAILWIND_VERSION=4.3.3
ARG TAILWIND_SHA256=dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a
RUN curl -fsSL -o /usr/local/bin/tailwindcss \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-linux-x64" \
    && echo "${TAILWIND_SHA256}  /usr/local/bin/tailwindcss" | sha256sum -c - \
    && chmod +x /usr/local/bin/tailwindcss
COPY static/src ./static/src
COPY templates ./templates
COPY core/forms.py core/templatetags/erp.py ./core/
RUN tailwindcss -i static/src/input.css -o /build/app.css --minify

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=css /build/app.css static/css/app.css

# Collected at build time so the container starts without writing to the image.
RUN DJANGO_DEBUG=0 DJANGO_SECRET_KEY=build-only-not-used-at-runtime-0123456789abcdef \
    python manage.py collectstatic --noinput

RUN useradd --create-home erp && chown -R erp /app
USER erp

EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", \
     "--access-logfile", "-", "--error-logfile", "-"]

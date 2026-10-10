FROM python:3.11-slim
WORKDIR /app
ARG VCS_REF=development
LABEL org.opencontainers.image.revision=$VCS_REF
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt ./
# antiword handles legacy binary .doc files. Its input is always a short-lived
# file under the worker's /tmp tmpfs; modern Office formats stay in memory.
RUN apt-get update \
    && apt-get install -y --no-install-recommends antiword \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
COPY docs ./docs
COPY scripts ./scripts
EXPOSE 39021
# OAuth callback query strings contain one-time credentials. Application
# access logging is disabled; proxy logs must also omit callback query args.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "39021", "--no-access-log"]

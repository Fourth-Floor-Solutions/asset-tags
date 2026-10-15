FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /srv
COPY requirements.txt .
# requirements.txt pins every package to an exact version and hash (generated from requirements.in).
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY app ./app
COPY CHANGELOG.md ./
# Optional: the git commit this image was built from, shown in the footer.
ARG GIT_COMMIT=""
ENV GIT_COMMIT=$GIT_COMMIT
COPY scripts ./scripts
RUN useradd -u 1000 -m tags && mkdir -p /data && chown tags /data
USER 1000
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
# Only your reverse proxy may set X-Forwarded-For (the sign-in limiter keys on
# client IP). Set FORWARDED_ALLOW_IPS to the proxy's address in .env.
ENV FORWARDED_ALLOW_IPS=127.0.0.1
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\""]

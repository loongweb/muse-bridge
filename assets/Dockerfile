# OPTIONAL — run the SAME serve.py on a free container host, NOT on the user's
# Mac. This is the "no VPS, but still always-on" option in references/no-vps.md.
#
# Build from the skill root (Docker context = skill directory):
#   docker build -f assets/Dockerfile -t muse-bridge .
#
# Runtime requirements:
#   * a PERSISTENT volume mounted at /data  (ephemeral free-tier disks lose mail)
#   * the bearer token supplied as a secret file at /run/secrets/mb_token
#     (or set MB_TOKEN_FILE to another mounted path). Never bake it into the image.
#   * HTTPS terminated by the platform; point config.http.base_url at that origin.
#
# Build ARG/ENV contain NO credentials. Do not add any.
FROM python:3.12-alpine

WORKDIR /app
COPY scripts/bridge.py scripts/serve.py ./

RUN mkdir -p /data/mail \
 && adduser -D -H -u 10001 mb \
 && chown -R mb /data

USER mb
EXPOSE 8787
ENV MB_ROOT=/data/mail \
    MB_TOKEN_FILE=/run/secrets/mb_token \
    PYTHONUNBUFFERED=1

CMD ["sh", "-c", "exec python3 serve.py --root \"$MB_ROOT\" --host 0.0.0.0 --port 8787 --token-file \"$MB_TOKEN_FILE\""]

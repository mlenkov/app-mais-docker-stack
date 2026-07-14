FROM python:3-alpine

RUN apk add --no-cache ca-certificates && \
    pip install --no-cache-dir pyyaml==6.0.2 && \
    rm -rf /root/.cache

COPY server.py /usr/local/bin/auth-server
COPY config.yml /etc/auth/config.yml

RUN chmod 755 /usr/local/bin/auth-server && \
    adduser -D -u 4180 auth && \
    chown auth:auth /etc/auth/config.yml

USER auth

EXPOSE 4180

ENTRYPOINT ["python3", "/usr/local/bin/auth-server"]

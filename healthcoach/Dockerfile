ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache \
    bash \
    python3 \
    py3-psycopg2 \
    py3-yaml \
    gcompat

COPY requirements.txt /tmp/
RUN apk add --no-cache --virtual .build-deps py3-pip \
    && pip3 install --no-cache-dir --prefer-binary --break-system-packages -r /tmp/requirements.txt \
    && apk del .build-deps \
    && rm -rf /tmp/requirements.txt /root/.cache

COPY scripts/ /app/scripts/
COPY config/ /app/config/
COPY db/ /app/db/
COPY grafana/ /app/grafana/
COPY .chainlit/ /app/.chainlit/
COPY public/ /app/public/
COPY chainlit.md /app/chainlit.md
COPY healthcoach/run.sh /app/run.sh

WORKDIR /app
RUN chmod +x /app/run.sh

CMD ["/app/run.sh"]

FROM python:3.13.7-slim

WORKDIR /app
RUN useradd --create-home --uid 10001 copperline \
    && mkdir -p /data \
    && chown -R copperline:copperline /app /data

COPY --chown=copperline:copperline . /app

USER copperline
ENV PYTHONPATH=/app/src
EXPOSE 8790

CMD ["python", "-m", "copperline", "--db", "/data/copperline.sqlite", "serve", "--host", "0.0.0.0", "--port", "8790"]

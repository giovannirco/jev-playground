FROM python:3.14.10-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app
COPY src ./src

RUN useradd --uid 1000 --create-home jev
USER 1000
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"
CMD ["python", "-m", "jev_playground.arcade"]

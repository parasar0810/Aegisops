FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=9000
WORKDIR /srv
RUN pip install --no-cache-dir "fastapi==0.115.12" "uvicorn==0.34.2" "prometheus-client==0.21.1" "httpx==0.28.1"
COPY main.py registration.py ./demo_app/
RUN useradd --create-home --uid 10001 demo
USER demo
EXPOSE 9000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/metrics', timeout=2)"
CMD ["uvicorn", "demo_app.main:app", "--host", "0.0.0.0", "--port", "9000"]

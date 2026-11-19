FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shopfront ./shopfront
ARG BUILD_TIME=0
ENV BUILD_TIME=${BUILD_TIME}
RUN useradd --create-home --uid 10002 shop
USER shop
EXPOSE 8000 8081
HEALTHCHECK --interval=15s --timeout=4s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/status', timeout=3)"
CMD ["python", "-m", "shopfront.supervisor"]

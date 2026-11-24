FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge ./bridge

ENV PYTHONUNBUFFERED=1 \
    CONFIG=/config/config.yaml

EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8099/health'); sys.exit(0)"
CMD ["python", "-m", "bridge"]

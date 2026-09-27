# Mystery Shopper engine: API + scheduled campaigns
FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mystery_shopper ./mystery_shopper
COPY config ./config
COPY examples ./examples
ENV MS_DB=/data/mystery_shopper.db \
    MS_CONFIG_DIR=/app/config
VOLUME ["/data"]
EXPOSE 8000
# ANTHROPIC_API_KEY and MS_API_TOKEN must be provided at runtime (never baked into the image)
CMD ["python", "-m", "mystery_shopper", "serve", "--host", "0.0.0.0", "--port", "8000", "--targets", "/app/config/targets.yaml"]

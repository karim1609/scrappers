FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget gnupg ca-certificates fonts-liberation \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 \
    libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 \
    libx11-xcb1 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libxss1 libxtst6 xdg-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY scrapers/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium

COPY scrapers/ ./scrapers/
COPY api.py /app/api.py
COPY tests/ ./tests/

RUN mkdir -p /app/output

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && sed -i 's/\r$//' /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
EXPOSE 8000
CMD ["-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]

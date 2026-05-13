FROM python:3.11-slim

WORKDIR /app

# Install Playwright system dependencies + ffmpeg (for audio conversion)
RUN apt-get update && apt-get install -y \
    wget gnupg libglib2.0-0 libnss3 libnspr4 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser for Playwright
RUN python -m playwright install chromium

COPY . .

ENV PORT=5000
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:$PORT app:app"]

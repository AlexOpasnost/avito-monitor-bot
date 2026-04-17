FROM python:3.13-slim

WORKDIR /app

# Chromium runtime deps + xvfb for headed mode on server
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates fonts-liberation \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 \
    libcairo2 libcups2 libdbus-1-3 libdrm2 libexpat1 libgbm1 \
    libglib2.0-0 libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 \
    libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
    libxkbcommon0 libxrandr2 xdg-utils \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium for Playwright
RUN python -m playwright install chromium

COPY . .

# xvfb-run provides a virtual display for headed Chromium on a headless server.
# If HEADLESS=true (default), Chromium runs headless and xvfb is harmless.
# If HEADLESS=false, xvfb gives it a virtual screen to render to.
CMD ["xvfb-run", "--auto-servernum", "python", "-u", "bot.py"]

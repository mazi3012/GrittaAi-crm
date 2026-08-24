# Hugging Face Space Dockerfile for Gretta AI Telegram Bot
FROM python:3.10-slim

# Install system dependencies (curl for health check verification)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy dependency definition and install Python packages
COPY requirements-bot.txt ./requirements-bot.txt
RUN pip install --no-cache-dir -r requirements-bot.txt

# Copy application code
COPY . .

# Expose default health-check port (HF Spaces uses 7860; Render overrides via PORT)
EXPOSE 7860

# Run the Telegram Bot
CMD ["python", "bot.py"]

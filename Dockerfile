# Hugging Face Space Dockerfile for Gretta AI Telegram Bot
FROM python:3.10-slim

# Install system dependencies required by PaddleOCR and OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy dependency definition and install Python packages
COPY requirements-bot.txt ./requirements-bot.txt
RUN pip install --no-cache-dir -r requirements-bot.txt

# Copy application code
COPY . .

# Expose port 7860 for Hugging Face Spaces health checks
EXPOSE 7860

# Run the Telegram Bot
CMD ["python", "bot.py"]

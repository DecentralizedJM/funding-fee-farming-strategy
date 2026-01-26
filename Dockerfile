FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/
COPY .env.example .

# Create data and logs directories
RUN mkdir -p data logs

# Set environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

# Run the bot
CMD ["python", "-m", "src.main"]

FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user
RUN useradd -m -u 1000 libex && chown -R libex:libex /app
USER libex

# Expose port
EXPOSE 3333

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3333"]
# Use Python 3.12 (compatible with scratchattach)
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Copy requirements first (for caching)
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Expose port (Render sets $PORT)
EXPOSE 10000

# Make start script executable
RUN chmod +x start.sh

# Start the service
CMD ["./start.sh"]

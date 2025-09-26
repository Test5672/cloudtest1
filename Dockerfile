# Use Python 3.10 (compatible with scratchattach)
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Copy requirements first (for caching)
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app
COPY . .

# Expose the port (Render sets $PORT automatically)
EXPOSE 10000

# Set environment variable for Render
ENV PORT=10000

# Make start script executable
RUN chmod +x start.sh

# Start the service
CMD ["./start.sh"]

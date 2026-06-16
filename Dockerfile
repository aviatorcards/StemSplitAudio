# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Install system dependencies (ffmpeg is required for yt-dlp and demucs)
# build-essential and gcc are included for compiling dependencies if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Create downloads directory
RUN mkdir -p downloads

# Expose port 5050 to the outside world
EXPOSE 5050

# Set environment variables
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

# Run the application
CMD ["python", "app.py"]

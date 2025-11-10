FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY models.py test_search_events.py ./

# Create logs directory
RUN mkdir -p logs

# Run tests
CMD ["python", "-m", "unittest", "test_search_events", "-v"]

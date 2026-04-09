FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY advisor.py background.py security.py state.py status_report.py mongot_doctor.py ./
COPY collectors/ collectors/
COPY engine/ engine/
COPY routes/ routes/
COPY frontend/ frontend/

EXPOSE 5050

# Default: in-cluster mode, port 5050.
# Override --uri, --namespace, --auth etc. via Deployment args or env.
CMD ["python3", "mongot_doctor.py", "--in-cluster", "--port", "5050"]

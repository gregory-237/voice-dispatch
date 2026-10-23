FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 libsndfile1 && rm -rf /var/lib/apt/lists/*
COPY requirements*.txt ./
RUN pip install --no-cache-dir torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cpu && pip install --no-cache-dir -r requirements-voice.txt
COPY . .
CMD ["sh","-ec","alembic upgrade head && python -m uvicorn dispatch.main:app --host 0.0.0.0 --port 8080"]

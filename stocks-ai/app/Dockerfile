FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY stocksai ./stocksai
ENV STOCKSAI_DB=/data/stocksai.db PYTHONUNBUFFERED=1
RUN mkdir -p /data && chown 1000:1000 /data
USER 1000:1000
EXPOSE 8000
CMD ["uvicorn", "stocksai.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python -m compileall -q .
ENV PORT=10000
CMD ["python","app.py"]

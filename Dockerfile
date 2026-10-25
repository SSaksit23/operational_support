# Cloud Run image for the web app. Build context is the project folder.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
# The OCR package pulls the desktop OpenCV, which needs system GUI libraries; the headless build does not.
RUN pip install --no-cache-dir -r requirements.txt \
 && pip uninstall -y opencv-python \
 && pip install --no-cache-dir opencv-python-headless
COPY ops_agent ops_agent
ENV PYTHONUNBUFFERED=1
# Sessions live in memory, so one instance; Cloud Run sets $PORT.
CMD exec uvicorn ops_agent.web.app:app --host 0.0.0.0 --port ${PORT:-8080}

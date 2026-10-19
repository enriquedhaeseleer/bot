# FundedBot on Railway (or any Docker host): the dashboard runs as the web process and keeps the bot running.
FROM python:3.12-slim
WORKDIR /app
COPY requirements-bot.txt .
RUN pip install --no-cache-dir -r requirements-bot.txt
COPY fundedbot ./fundedbot
# /data = a Railway volume: state, log and pid files survive restarts and redeploys
ENV PYTHONUNBUFFERED=1 FP_DATA_DIR=/data FP_AUTOSTART=1
CMD ["sh", "-c", "python -m fundedbot dashboard --host 0.0.0.0 --port ${PORT:-8787}"]

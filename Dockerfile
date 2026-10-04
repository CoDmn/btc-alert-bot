FROM python:3.12-slim
WORKDIR /app
# tzdata : affichage des heures en Europe/Paris
RUN pip install --no-cache-dir tzdata
COPY btc_alert_bot.py config.toml test_scenarios.py ./
ENV DATA_DIR=/app/data PYTHONUNBUFFERED=1
CMD ["python", "btc_alert_bot.py", "--config", "/app/config.toml"]

FROM python:3.12-slim

WORKDIR /app
COPY swt_flask_project/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY swt_flask_project/ /app
COPY config/pump_control.json /config/pump_control.json

EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "server:app"]

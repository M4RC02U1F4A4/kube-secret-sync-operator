
FROM python:3.10-slim
LABEL org.opencontainers.image.source=https://github.com/M4RC02U1F4A4/kube-secret-sync-operator
COPY main.py /app/main.py
COPY requirements.txt /app/requirements.txt
WORKDIR /app
RUN pip install -r requirements.txt

CMD ["kopf", "run", "--all-namespaces", "main.py"]
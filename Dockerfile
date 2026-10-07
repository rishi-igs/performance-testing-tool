FROM python:3.12-slim

ARG JMETER_VERSION=5.6.3
RUN apt-get update \
 && apt-get install -y --no-install-recommends openjdk-17-jre-headless curl ca-certificates \
 && curl -fsSL "https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-${JMETER_VERSION}.tgz" -o /tmp/jmeter.tgz \
 && tar -xzf /tmp/jmeter.tgz -C /opt && rm /tmp/jmeter.tgz \
 && ln -s /opt/apache-jmeter-${JMETER_VERSION}/bin/jmeter /usr/local/bin/jmeter \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend backend
COPY frontend frontend
COPY config config

ENV PERF_DATA_DIR=/data JMETER_BIN=jmeter
VOLUME /data
EXPOSE 8000
WORKDIR /app/backend
# Set API_KEY for anything beyond a laptop. The tool refuses private targets unless allowed.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

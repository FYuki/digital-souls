FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04@sha256:8aef630a54bc5c5146ae5ce68e6af5caa3df0fb690bb91544175c91f307e4356

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY whisper_service/requirements.txt /tmp/whisper-requirements.txt
RUN python3 -m pip install --break-system-packages --no-cache-dir \
    --requirement /tmp/whisper-requirements.txt
ARG WHISPER_MODEL_REVISION=08e178d48790749d25932bbc082711ddcfdfbc4f
ENV WHISPER_BAKED_MODEL_REVISION=${WHISPER_MODEL_REVISION}
RUN python3 -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='Systran/faster-whisper-medium', revision='$WHISPER_MODEL_REVISION', local_dir='/opt/models/whisper-medium')"
COPY whisper_service /app/whisper_service

RUN chmod -R a+rX /opt/models/whisper-medium
USER 10001:10001
EXPOSE 50022
ENTRYPOINT ["python3", "-m", "uvicorn", "whisper_service.app:app"]
CMD ["--host", "127.0.0.1", "--port", "50022", "--workers", "1", "--no-access-log"]

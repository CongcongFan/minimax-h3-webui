FROM pytorch/pytorch:2.9.1-cuda13.0-cudnn9-runtime@sha256:60f22fb80755fd0b470fb47928dbd55816aa9f847edd95cf43c93253507a9ddf

LABEL org.opencontainers.image.source="https://github.com/onigirikiller/minimax-h3-webui" \
      org.opencontainers.image.description="MiniMax H3 按需生产执行器（不含模型与用户数据）" \
      org.opencontainers.image.licenses="MIT"

ARG COMFYUI_COMMIT=8a33128f2f8c5585c57486c07de481241e70a39c
ARG SEEDVR2_COMMIT=6d50cf04803aa6d27300ccf2724d5471d366eb12
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    HF_HOME=/workspace/hf-cache \
    H3_RUNTIME=/workspace/h3-runtime \
    COMFYUI_MODELS=/opt/ComfyUI/models

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg git libgl1 libglib2.0-0 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/Comfy-Org/ComfyUI.git /opt/ComfyUI \
    && git -C /opt/ComfyUI checkout "$COMFYUI_COMMIT" \
    && pip install --no-cache-dir -r /opt/ComfyUI/requirements.txt

RUN git clone https://github.com/comfyorg/comfyui_seedvr2.git /opt/ComfyUI/custom_nodes/comfyui_seedvr2 \
    && git -C /opt/ComfyUI/custom_nodes/comfyui_seedvr2 checkout "$SEEDVR2_COMMIT" \
    && pip install --no-cache-dir -r /opt/ComfyUI/custom_nodes/comfyui_seedvr2/requirements.txt

COPY cloud/gateway/requirements.txt /opt/h3/requirements.txt
RUN pip install --no-cache-dir -r /opt/h3/requirements.txt

COPY h3_backend.py /opt/h3/h3_backend.py
COPY cloud/gateway/ /opt/h3/

RUN python /opt/h3/bootstrap_models.py --self-test \
    && python -m py_compile \
      /opt/h3/h3_backend.py \
      /opt/h3/main.py \
      /opt/h3/bootstrap_models.py \
      /opt/h3/start.py

EXPOSE 8000
CMD ["python", "/opt/h3/start.py"]

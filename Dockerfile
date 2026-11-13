FROM runpod/worker-comfyui:5.8.4-base

ARG HF_TOKEN=""

# Install curl (needed by start.sh hot-patch) and git
RUN apt-get update && apt-get install -y --no-install-recommends curl git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir websocket-client

# Replace ComfyUI with latest version — 5.8.4-base ships an older build
# that is missing MiniMaxH3ImageConditioning (requires 0.45.3+).
# comfy-cli (used below for model downloads) is a separate package and is unaffected.
RUN rm -rf /comfyui \
    && git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git /comfyui \
    && pip install -r /comfyui/requirements.txt --quiet

# ── Models ────────────────────────────────────────────────────────────────────
# Source: https://huggingface.co/Comfy-Org/MiniMax-H3-Image
# Total ~49 GB — downloaded once during docker build, baked into the image.
#
# NOTE: RunPod GitHub builds have a 30-min Docker build step timeout.
# 49 GB over that window requires ~27 MB/s sustained. If the build times out,
# build locally and push to Docker Hub instead:
#   docker build --platform linux/amd64 -t yourname/minimax-h3-image-serverless:v1 .
#   docker push yourname/minimax-h3-image-serverless:v1
#
# GPU recommendation: 48GB (A6000 / L40S) for best speed — the 27 GB text
# encoder fits in VRAM. 24GB (RTX 4090) also works via CPU offload (slower).

# 1. Diffusion model — int8_convrot pruned (~21 GB)
RUN BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy model download \
      --url 'https://huggingface.co/Comfy-Org/MiniMax-H3-Image/resolve/main/diffusion_models/minimax_h3_image_pruned_int8_convrot.safetensors' \
      --relative-path models/diffusion_models \
      --filename 'minimax_h3_image_pruned_int8_convrot.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i) && sleep $SLEEP; done

# 2. Text encoder — Qwen3-VL 32B int8_convrot (~27 GB)
#    Workflow loads it from text_encoders/minimax-h3/ subdirectory.
RUN mkdir -p /comfyui/models/text_encoders/minimax-h3 \
    && BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy model download \
      --url 'https://huggingface.co/Comfy-Org/MiniMax-H3-Image/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors' \
      --relative-path models/text_encoders/minimax-h3 \
      --filename 'qwen3vl_32b_minimax_h3_int8_convrot.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i) && sleep $SLEEP; done

# 3. VAE — image VAE fp16 (~1.2 GB)
#    Workflow loads it from vae/minimax-h3/ subdirectory.
RUN mkdir -p /comfyui/models/vae/minimax-h3 \
    && BACKOFFS="10 20 30 60 90" && for i in 1 2 3 4 5; do \
    comfy model download \
      --url 'https://huggingface.co/Comfy-Org/MiniMax-H3-Image/resolve/main/vae/minimax_h3_image_vae_fp16.safetensors' \
      --relative-path models/vae/minimax-h3 \
      --filename 'minimax_h3_image_vae_fp16.safetensors' && break; \
    if [ $i -eq 5 ]; then echo "model-download failed" >&2; exit 1; fi; \
    SLEEP=$(echo $BACKOFFS | cut -d ' ' -f $i) && sleep $SLEEP; done

# ── Handler & workflow ────────────────────────────────────────────────────────
# start.sh hot-patches these from GitHub on every boot,
# so handler/workflow changes never require a full rebuild.
COPY rp_handler.py     /rp_handler.py
COPY api-workflow.json /api-workflow.json
COPY start.sh          /start.sh
RUN chmod +x /start.sh

CMD ["/start.sh"]

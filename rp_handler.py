"""
RunPod Serverless Handler — MiniMax H3 Image Edit (ComfyUI)

Workflow: MiniMax H3 image editing with up to 4 reference images.

API input schema:
  {
    "prompt":    str,         # Required. Describe the edit / generation.
    "image":     str,         # Required. Base64-encoded primary image (PNG or JPG).
    "image_url": str,         # Alternative to "image" — URL to fetch image from.
    "image2":    str,         # Optional. Second reference image (base64). Defaults to image.
    "image3":    str,         # Optional. Third reference image (base64). Defaults to image.
    "image4":    str,         # Optional. Fourth reference image (base64). Defaults to image.
    "seed":      int,         # Optional. -1 = random. Default: -1
    "steps":     int,         # Optional. Default: 6
    "cfg":       float,       # Optional. Default: 1.0
    "denoise":   float        # Optional. Default: 1.0
  }

API output schema:
  { "image": str }            # Base64-encoded output PNG

Node map (api-workflow.json):
  "43" — PrimitiveStringMultiline  — prompt text
  "74" — LoadImage                 — image 1 (primary, goes through resize pipeline)
  "75" — LoadImage                 — image 2
  "76" — LoadImage                 — image 3
  "77" — LoadImage                 — image 4
  "38" — KSampler                  — seed, steps, cfg, denoise
  "73" — SaveImage                 — output
"""

import runpod
import os
import websocket
import base64
import json
import uuid
import logging
import urllib.request
import urllib.parse
import urllib.error
import subprocess
import binascii
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SERVER_ADDRESS = os.getenv("SERVER_ADDRESS", "127.0.0.1")
CLIENT_ID = str(uuid.uuid4())

# ComfyUI input directory
INPUT_DIR = "/comfyui/input"
os.makedirs(INPUT_DIR, exist_ok=True)

# Load workflow template once at startup
WORKFLOW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api-workflow.json")
with open(WORKFLOW_PATH) as f:
    WORKFLOW_TEMPLATE = json.load(f)

# Node IDs (match api-workflow.json)
NODE_PROMPT   = "43"   # PrimitiveStringMultiline — prompt value
NODE_IMG1     = "74"   # LoadImage — primary image (goes through resize pipeline)
NODE_IMG2     = "75"   # LoadImage — image 2
NODE_IMG3     = "76"   # LoadImage — image 3
NODE_IMG4     = "77"   # LoadImage — image 4
NODE_SAMPLER  = "38"   # KSampler — seed, steps, cfg, denoise


# ──────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────

def save_b64_image(b64_data: str, path: str) -> None:
    try:
        decoded = base64.b64decode(b64_data)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(decoded)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"Base64 decode failed: {e}")


def download_url_to_file(url: str, path: str) -> None:
    result = subprocess.run(
        ["wget", "-q", "-O", path, url],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"wget failed: {result.stderr}")


def queue_prompt(prompt: dict) -> str:
    url = f"http://{SERVER_ADDRESS}:8188/prompt"
    payload = json.dumps({"prompt": prompt, "client_id": CLIENT_ID}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())["prompt_id"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        logger.error(f"ComfyUI /prompt rejected: {body}")
        raise RuntimeError(f"ComfyUI 400: {body}")


def get_image_bytes(filename: str, subfolder: str, folder_type: str) -> bytes:
    params = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": folder_type})
    with urllib.request.urlopen(f"http://{SERVER_ADDRESS}:8188/view?{params}") as r:
        return r.read()


def get_history(prompt_id: str) -> dict:
    with urllib.request.urlopen(f"http://{SERVER_ADDRESS}:8188/history/{prompt_id}") as r:
        return json.loads(r.read())


# ──────────────────────────────────────────────
# WebSocket execution tracker
# ──────────────────────────────────────────────

def run_workflow_via_websocket(ws: websocket.WebSocket, prompt: dict) -> dict:
    """Queue prompt, wait for completion via websocket, return output images."""
    prompt_id = queue_prompt(prompt)
    logger.info(f"Queued prompt_id={prompt_id}")

    while True:
        raw = ws.recv()
        if not isinstance(raw, str):
            continue   # binary frame — skip
        msg = json.loads(raw)
        if msg["type"] == "executing":
            data = msg["data"]
            if data["node"] is None and data["prompt_id"] == prompt_id:
                logger.info("ComfyUI execution complete")
                break
        elif msg["type"] == "execution_error":
            raise RuntimeError(f"ComfyUI execution error: {msg['data']}")

    history = get_history(prompt_id).get(prompt_id, {})
    output_images = {}
    for node_id, node_out in history.get("outputs", {}).items():
        if "images" in node_out:
            imgs = []
            for img_info in node_out["images"]:
                raw_bytes = get_image_bytes(
                    img_info["filename"],
                    img_info.get("subfolder", ""),
                    img_info.get("type", "output"),
                )
                imgs.append(base64.b64encode(raw_bytes).decode("utf-8"))
            output_images[node_id] = imgs

    return output_images


# ──────────────────────────────────────────────
# RunPod handler
# ──────────────────────────────────────────────

def handler(job: dict) -> dict:
    job_input = job.get("input", {})
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    logger.info(f"[{task_id}] input keys: {list(job_input.keys())}")

    # ── Resolve primary image (required) ──
    img1_path = f"{INPUT_DIR}/{task_id}_img1.png"

    if "image" in job_input:
        try:
            save_b64_image(job_input["image"], img1_path)
        except ValueError as e:
            return {"error": str(e)}
    elif "image_base64" in job_input:
        try:
            save_b64_image(job_input["image_base64"], img1_path)
        except ValueError as e:
            return {"error": str(e)}
    elif "image_url" in job_input:
        try:
            download_url_to_file(job_input["image_url"], img1_path)
        except RuntimeError as e:
            return {"error": str(e)}
    else:
        return {"error": "Provide 'image' (base64), 'image_base64', or 'image_url'."}

    # ── Resolve optional extra images (default to img1 if not provided) ──
    def resolve_optional_image(key: str, suffix: str) -> str:
        if key in job_input:
            path = f"{INPUT_DIR}/{task_id}_{suffix}.png"
            try:
                save_b64_image(job_input[key], path)
            except ValueError as e:
                raise ValueError(f"{key} decode failed: {e}")
            return path
        return img1_path  # reuse primary image

    try:
        img2_path = resolve_optional_image("image2", "img2")
        img3_path = resolve_optional_image("image3", "img3")
        img4_path = resolve_optional_image("image4", "img4")
    except ValueError as e:
        return {"error": str(e)}

    # ── Parameters ──
    prompt_text = job_input.get("prompt", "").strip()
    if not prompt_text:
        return {"error": "'prompt' is required."}

    seed    = int(job_input.get("seed", -1))
    steps   = int(job_input.get("steps", 6))
    cfg     = float(job_input.get("cfg", 1.0))
    denoise = float(job_input.get("denoise", 1.0))

    if seed == -1:
        import random
        seed = random.randint(0, 2**32 - 1)

    logger.info(f"[{task_id}] prompt={prompt_text!r}, seed={seed}, steps={steps}, cfg={cfg}")

    # ── Patch workflow ──
    workflow = json.loads(json.dumps(WORKFLOW_TEMPLATE))   # deep copy
    workflow[NODE_PROMPT]["inputs"]["value"]    = prompt_text
    workflow[NODE_IMG1]["inputs"]["image"]      = img1_path
    workflow[NODE_IMG2]["inputs"]["image"]      = img2_path
    workflow[NODE_IMG3]["inputs"]["image"]      = img3_path
    workflow[NODE_IMG4]["inputs"]["image"]      = img4_path
    workflow[NODE_SAMPLER]["inputs"]["seed"]    = seed
    workflow[NODE_SAMPLER]["inputs"]["steps"]   = steps
    workflow[NODE_SAMPLER]["inputs"]["cfg"]     = cfg
    workflow[NODE_SAMPLER]["inputs"]["denoise"] = denoise

    # ── Connect WebSocket (retry up to 5 min — large models take time to load) ──
    ws_url = f"ws://{SERVER_ADDRESS}:8188/ws?clientId={CLIENT_ID}"
    ws = websocket.WebSocket()
    for attempt in range(60):
        try:
            ws.connect(ws_url)
            logger.info(f"WebSocket connected (attempt {attempt + 1})")
            break
        except Exception as e:
            logger.warning(f"WS connect attempt {attempt + 1}/60 failed: {e}")
            if attempt == 59:
                return {"error": "WebSocket connection timeout — ComfyUI may still be loading models"}
            time.sleep(5)

    # ── Run workflow ──
    try:
        images = run_workflow_via_websocket(ws, workflow)
    except RuntimeError as e:
        ws.close()
        return {"error": str(e)}
    finally:
        ws.close()

    if not images:
        return {"error": "No images returned from ComfyUI"}

    # Return first output image
    for node_id, node_images in images.items():
        if node_images:
            logger.info(f"[{task_id}] Done — returning image from node {node_id}")
            return {"image": node_images[0]}

    return {"error": "No output image found in ComfyUI history"}


runpod.serverless.start({"handler": handler})

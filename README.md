# SPARK

Automated short-form video production pipeline (local GPU · Python · YouTube/TikTok/Instagram).

---

## Table of contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Requirements](#requirements)
4. [Setup](#setup)
5. [Configuration reference](#configuration-reference)
6. [Running the pipeline](#running-the-pipeline)
7. [Multi-niche profiles](#multi-niche-profiles)
8. [Multi-platform distribution](#multi-platform-distribution)
9. [Analytics feedback loop](#analytics-feedback-loop)
10. [YouTube AI compliance](#youtube-ai-compliance)
11. [Run lifecycle and timings](#run-lifecycle-and-timings)
12. [Interpreting run manifests](#interpreting-run-manifests)
13. [Known limits](#known-limits)
14. [Troubleshooting](#troubleshooting)
15. [Tests](#tests)
16. [License](#license)

---

## Overview

SPARK takes a trending topic, generates a 50–60 s French narration script with Gemini 2.5 Flash, renders matching cinematic clips via Wan2.1-T2V-1.3B (HuggingFace Diffusers) on a local or cloud GPU, synthesizes the voice with Edge TTS (Microsoft Neural TTS, default) or Kokoro ONNX (local fallback), assembles the final 1080×1920 Short with FFmpeg, and uploads it to YouTube — entirely unattended.

Each run produces a JSON manifest (`logs/run_{id}_manifest.json`) with every artefact path, timing, and metadata for auditing.

---

## Architecture

```
SPARK/
├── core/
│   ├── trend_hunter.py     — Google Trends (SerpAPI / pytrends fallback) + Reddit scoring
│   ├── script_gen.py       — Gemini 2.0 Flash narration script + JSON schema validation
│   ├── video_gen.py        — Wan2.1-T2V-1.3B clip rendering (HuggingFace Diffusers), static fallback
│   ├── audio_gen.py        — Edge TTS (default) + Kokoro ONNX (local fallback), with retry
│   ├── post_prod.py        — FFmpeg assembly: clips + narration + subtitles
│   ├── thumbnail_gen.py    — Frame extraction + Gemini Vision overlay
│   ├── uploader.py         — YouTube Data API v3 (OAuth2 Fernet)
│   ├── tiktok_uploader.py  — TikTok Content Posting API v2 (FILE_UPLOAD)
│   ├── instagram_uploader.py — Instagram Graph API (Reels container + publish)
│   ├── analytics_reader.py — YouTube Analytics API (deferred, cached)
│   ├── history.py          — config / env loading, publish_history.jsonl R/W
│   └── ...
├── dashboard/
│   ├── app.py              — FastAPI control panel (SSE, /generate, /analytics/summary)
│   └── ui/                 — HTML player + main dashboard
├── profiles/               — Per-niche YAML overrides
│   ├── ia_histoire.yaml
│   └── finance_perso.yaml
├── prompts/                — System prompts for Gemini (script + video)
├── secrets/                — OAuth2 tokens (not committed, gitignored)
├── tests/                  — 130+ unit tests
├── pipeline.py             — End-to-end async orchestrator
├── scheduler.py            — APScheduler (cron, per-profile)
└── config.yaml             — All pipeline parameters
```

---

## Requirements

| Component | Version | Notes |
|-----------|---------|-------|
| Python | 3.11+ | |
| FFmpeg | 6+ | Must be in PATH or set `ffmpeg_bin` in config.yaml |
| NVIDIA GPU | ≥ 8 GB VRAM | Wan2.1-T2V-1.3B (float16). T4 (Colab free tier) fully supported. CPU fallback very slow. |
| CUDA | 12.x | |
| PyTorch | 2.3+ | With CUDA 12 wheels |

Minimum disk space: 30 GB (models + outputs).

---

## Setup

```powershell
# 1. Clone the repository
git clone https://github.com/Blockprod/SPARK.git
cd SPARK

# 2. Create virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1    # Windows PowerShell

# 3. Install dependencies
pip install -r requirements.txt

# 4. Download model files (one-time)
# Wan2.1-T2V-1.3B — downloaded automatically on first run (~8 GB), or pre-download:
python -c "from huggingface_hub import snapshot_download; snapshot_download('Wan-AI/Wan2.1-T2V-1.3B-Diffusers')"

# Kokoro TTS (82M ONNX) — ~330 MB
python -c "from huggingface_hub import hf_hub_download; hf_hub_download('hexgrad/Kokoro-82M', 'kokoro-v1.0.fp16.onnx', local_dir='models'); hf_hub_download('hexgrad/Kokoro-82M', 'voices-v1.0.bin', local_dir='models')"

# 5. Configure credentials
cp .env.example .env
# Open .env and fill in required keys (see Configuration reference)

# 6. YouTube OAuth2 — grants upload + analytics access
python -c "from core.uploader import _run_oauth_flow; _run_oauth_flow()"
# Saves token to secrets/youtube_token.json

# 7. Smoke test
python -m pytest tests/ -q
```

---

## Configuration reference

All pipeline behaviour is controlled by two files:

- `.env` — API keys and secrets (**never commit this file**)
- `config.yaml` — pipeline parameters, model settings, scheduling

### Required `.env` variables

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google AI Studio key for script generation |
| `YOUTUBE_CLIENT_SECRETS_FILE` | Path to `secrets/client_secret.json` |
| `YOUTUBE_TOKEN_FILE` | Path to OAuth2 token (generated at first run) |

### Optional `.env` variables

| Variable | Description |
|----------|-------------|
| `MISTRAL_API_KEY` | Optional — reserved for future Voxtral TTS integration. Not required for Edge TTS or Kokoro. |
| `SERPAPI_KEY` | Official Google Trends API. Replaces pytrends scraping when set. Recommended for production. Plan gratuit : 100 req/mois → https://serpapi.com |
| `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` | Reddit OAuth2 for topic discovery |
| `REDDIT_USER_AGENT` | Reddit API user-agent string |
| `TIKTOK_ACCESS_TOKEN` | TikTok Content Posting API — requires TikTok Developer account |
| `INSTAGRAM_ACCESS_TOKEN` | Instagram Graph API long-lived token |
| `INSTAGRAM_USER_ID` | Instagram Business/Creator account ID |
| `INSTAGRAM_VIDEO_HOST_URL` | HTTPS base URL where MP4s are publicly reachable (required for Instagram upload) |

### Key `config.yaml` sections

#### `pipeline`
- `min_duration_sec` / `max_duration_sec` — Short duration constraints (default 50–60 s)
- `max_scenes` — Maximum scene count (default 8)
- `keep_intermediate_files` — Set `true` to preserve clips/audio after each run
- `global_timeout_sec` — Hard timeout for a full run (default 7200 s / 2 h)

#### `script_generation`
- `model` — Gemini model (default `gemini-2.5-flash`)
- `template_pool` — Narrative templates rotated per run. After 14+ runs, the template with the best `avg_view_percentage` is automatically preferred.

#### `audio_generation`
- `active_backend` — `edge_tts` (default, free, no GPU) or `kokoro` (local ONNX fallback)
- `edge_tts.voice` — Microsoft Neural TTS voice (default `fr-FR-DeniseNeural`)
- `edge_tts.rate` — Speech rate adjustment (default `-5%` for clearer articulation)

#### `platforms`
- `youtube.enabled` — primary upload (default `true`)
- `tiktok.enabled` — set `true` + `TIKTOK_ACCESS_TOKEN` in `.env`
- `instagram.enabled` — set `true` + Instagram credentials in `.env`

#### `scheduler`
- `default_publish_slots` — list of `"HH:MM"` FR timezone peak slots
- `max_shorts_per_week` — weekly quota guard (default 7)
- `profiles.{name}.enabled` / `publish_slots` — per-niche cron slots

---

## Running the pipeline

```powershell
# Single run — topic auto-selected from trends
python pipeline.py

# Single run — explicit topic
python pipeline.py --topic "L'IA générative à travers l'histoire"

# Single run + upload to YouTube
python pipeline.py --upload

# Single run with a specific niche profile
python pipeline.py --profile finance_perso

# Start automated scheduler (peak slot cron)
python scheduler.py

# Launch dashboard (web control panel)
uvicorn dashboard.app:app --host 127.0.0.1 --port 8000
```

**Dashboard API**

| Endpoint | Description |
|----------|-------------|
| `GET /trends` | Current ranked topic candidates |
| `POST /generate` | Start a pipeline run (`{"topic": "...", "upload": true, "profile": "ia_histoire"}`) |
| `GET /status/{run_id}` | SSE stream of run progress |
| `GET /preview/{run_id}` | Video paths and metadata |
| `POST /upload/{run_id}` | Trigger YouTube upload for a completed run |
| `GET /analytics/summary` | Aggregated KPIs by template from `performance_cache.jsonl` |
| `GET /runs` | List all known runs |

---

## Multi-niche profiles

Profiles let you run multiple niches on the same GPU without config file conflicts.

```powershell
# Run the finance_perso profile
python pipeline.py --profile finance_perso
```

When a profile is active:
- `profiles/{profile}.yaml` is deep-merged over `config.yaml`
- Logs are isolated to `logs/{profile}/`
- Outputs are isolated to `outputs/{profile}/`

To add a new niche, create `profiles/{your_niche}.yaml`:

```yaml
project:
  niche: ma_niche

trends:
  reddit:
    subreddits:
      - mysub1
      - mysub2

script_generation:
  narration_style: "ton, style, ambiance"
  template_pool:
    - revelation
    - countdown

uploader:
  default_tags:
    - Tag1
    - Tag2
    - Shorts
```

To schedule it automatically, add to `config.yaml`:

```yaml
scheduler:
  profiles:
    ma_niche:
      enabled: true
      publish_slots:
        - "19:00"
```

---

## Multi-platform distribution

Enable platforms in `config.yaml`:

```yaml
platforms:
  youtube:
    enabled: true
  tiktok:
    enabled: true   # requires TIKTOK_ACCESS_TOKEN
  instagram:
    enabled: true   # requires INSTAGRAM_ACCESS_TOKEN + INSTAGRAM_VIDEO_HOST_URL
```

Each platform runs independently — a failure on TikTok or Instagram does **not** block the YouTube upload. Errors are logged as warnings and stored in `run_ctx["tiktok_error"]` / `run_ctx["instagram_error"]`.

**Instagram note**: The Graph API requires the video to be publicly accessible via HTTPS. Set `INSTAGRAM_VIDEO_HOST_URL` to a CDN or static server base URL.

---

## Analytics feedback loop

After each successful YouTube upload, SPARK schedules a deferred analytics fetch (48 h delay to allow view accumulation). Results are written to `logs/performance_cache.jsonl`.

At the next pipeline run:
- The template with the best `avg_view_percentage` over the last 14 runs is automatically preferred in script generation.
- Topics matching historically high-performing keywords receive a score bonus in trend ranking.

View aggregated analytics:
```
GET /analytics/summary
```

Returns:
```json
{
  "by_template": {
    "revelation": {"avg_view_pct": 48.2, "runs": 6, "total_views": 3400},
    "countdown":  {"avg_view_pct": 31.5, "runs": 4, "total_views": 1800}
  },
  "total_runs": 10,
  "last_fetched_at": "2026-04-15T14:30:00+00:00"
}
```

---

## YouTube AI compliance

As of 2024, YouTube requires creators to declare AI-generated or AI-altered content. SPARK handles this automatically:

1. **API metadata**: every upload sets `containsSyntheticMedia: true` in the video status body (via YouTube Data API v3).
2. **Description disclosure**: the system prompt instructs Gemini to end every `youtube_description` with the sentence `"Contenu généré avec l'aide de l'IA."` — this satisfies YouTube's text disclosure requirement.
3. **Internal flag**: `payload["ai_generated"] = True` is added to the script payload for internal traceability.

No manual action required. Verify compliance by checking the "Content disclosure" setting in YouTube Studio after upload.

---

## Run lifecycle and timings

| Stage | Module | Typical duration |
|-------|--------|-----------------|
| 1. Trend discovery | `trend_hunter.py` | 5–30 s |
| 2. Script generation | `script_gen.py` (Gemini) | 10–30 s |
| 3. Video generation | `video_gen.py` (Wan2.1-T2V-1.3B) | 5–20 min (GPU, T4+) |
| 4. Audio synthesis | `audio_gen.py` (Kokoro / Voxtral) | 5–30 s |
| 5. Post-production | `post_prod.py` (FFmpeg) | 30–90 s |
| 6. Upload | `uploader.py` + platforms | 30–120 s |
| 7. Thumbnail | `thumbnail_gen.py` | 10–20 s |

Total: **15–50 min** per run depending on GPU and number of scenes.

If a scene fails on GPU (VRAM error, timeout), `video_gen.py` automatically generates a static fallback clip (black frame, no GPU required) and logs `degraded_scenes` in the manifest.

---

## Interpreting run manifests

Each run writes `logs/run_{id}_manifest.json`:

```json
{
  "run_id": "abc123def456",
  "started_at": "2026-04-10T12:30:00+00:00",
  "finished_at": "2026-04-10T13:05:17+00:00",
  "status": "success",
  "topic": "L'IA dans la Deuxième Guerre Mondiale",
  "profile": "ia_histoire",
  "template_used": "revelation",
  "scenes": 6,
  "duration_sec": 58,
  "degraded_scenes": 0,
  "youtube_video_id": "dQw4w9WgXcQ",
  "youtube_url": "https://youtu.be/dQw4w9WgXcQ",
  "tiktok_publish_id": "...",
  "instagram_media_id": "..."
}
```

Key fields:
- `status` — `success`, `error`, or `timeout`
- `degraded_scenes` — number of scenes that fell back to static frames (0 = fully rendered)
- `template_used` — narrative template selected (fed back into analytics loop)
- `error` — present only when `status != "success"`

---

## Known limits

| Limit | Value | Workaround |
|-------|-------|------------|
| YouTube Data API quota | 10 000 units/day | 1 upload ≈ 1600 units → max ~6 uploads/day on free tier |
| Wan2.1-T2V-1.3B VRAM | ≥ 8 GB required (T4+) | Static fallback activates automatically on OOM |
| Voxtral (Mistral) rate limit | 429 → 4 retries with exp backoff | Automatic — no action needed |
| SerpAPI free plan | 100 queries/month | Paid plans start at $50/month (5000 queries) |
| pytrends (fallback) | Unofficial — may return 404 or 429 | Set `SERPAPI_KEY` to switch to official API |
| Instagram upload | Requires public HTTPS video URL | Set `INSTAGRAM_VIDEO_HOST_URL` in `.env` |
| TikTok upload size | Max 128 MiB per video | 60 s Shorts at 1080p are typically 30–80 MiB |

---

## Troubleshooting

### `Google Trends request failed: ResponseError`
pytrends returned a 404 or 429 from Google. This is expected when the scraping is blocked.
**Fix**: Set `SERPAPI_KEY` in `.env` to switch to the official SerpAPI endpoint.

### `ScriptValidationError: duration_sec must be between 50 and 60`
Gemini returned an out-of-range duration. The pipeline retries automatically (up to 3 times).
If it persists, relax `min_duration_sec` / `max_duration_sec` in `config.yaml`.

### `VideoGenerationError: CUDA out of memory`
GPU VRAM exhausted. The scene is automatically replaced with a static fallback frame.
To avoid this: reduce `max_scenes` in `config.yaml`, or lower `num_inference_steps` in `video_generation`, or enable `use_cpu_offload: true`.

### `AnalyticsError: OAuth2 token file not found`
Run an upload first to generate the token: `python pipeline.py --upload`
Or regenerate: `python -c "from core.uploader import _run_oauth_flow; _run_oauth_flow()"`

### `TikTokUploaderError: TIKTOK_ACCESS_TOKEN is absent`
Set `TIKTOK_ACCESS_TOKEN` in `.env` and `platforms.tiktok.enabled: true` in `config.yaml`.

### `InstagramUploaderError: INSTAGRAM_VIDEO_HOST_URL is absent`
Instagram requires the video to be accessible via a public HTTPS URL.
Set `INSTAGRAM_VIDEO_HOST_URL` to a CDN or static server base URL in `.env`.

### Pipeline run times out after 2 h
Increase `global_timeout_sec` in `config.yaml` (e.g. `10800` for 3 h).
This is most common when generating many scenes on a slow GPU.

---

## Tests

```powershell
# Activate environment first
.venv\Scripts\Activate.ps1

# Run full test suite
python -m pytest tests/ -v

# Run a single module
python -m pytest tests/test_trend_hunter.py -v
```

All tests are unit tests — no network calls, no GPU required.

---

## License

Private — all rights reserved.

### Model licenses

| Model | License | Commercial use | Attribution required |
|-------|---------|----------------|---------------------|
| Kokoro-82M (hexgrad) | Apache 2.0 | ✅ Free | No |
| Wan2.1-T2V-1.3B (Wan-AI) | Apache 2.0 | ✅ Free | No |

Full audit: [tasks/audits/methode/audit_licences_models_2026-04-11.md](tasks/audits/methode/audit_licences_models_2026-04-11.md)

# SPARK

Automated short-form video production pipeline.

---

## Overview

SPARK is a local Python pipeline that automates the end-to-end creation and publishing of short-form videos. It handles everything from topic discovery to final delivery, with minimal human intervention.

## Requirements

- Python 3.11+
- FFmpeg in PATH (or configured in `config.yaml`)
- NVIDIA GPU recommended for video generation

## Setup

```bash
# 1. Clone the repository
git clone https://github.com/Blockprod/SPARK.git
cd SPARK

# 2. Create and activate virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1   # Windows PowerShell

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
copy .env.example .env
# Edit .env and fill in your API keys

# 5. Configure pipeline
# Edit config.yaml to match your setup
```

## Configuration

All pipeline behaviour is controlled via two files:

- `.env` — API keys and secrets (never committed)
- `config.yaml` — pipeline parameters, model settings, scheduling

## Running

```bash
# Run pipeline once
python pipeline.py

# Start scheduler (automated runs)
python scheduler.py

# Launch dashboard
uvicorn dashboard.app:app --host 127.0.0.1 --port 8000
```

## Tests

```bash
python -m pytest tests/ -v
```

## Project Structure

```
SPARK/
├── core/           # Pipeline modules
├── dashboard/      # Web control panel
├── prompts/        # AI prompt templates
├── secrets/        # OAuth credentials (not committed)
├── tests/          # Unit tests
├── config.yaml     # Pipeline configuration
└── .env            # Environment secrets (not committed)
```

## License

Private — all rights reserved.

# Running Drafts backend

This repository contains two independently operated services:

- [`app/`](app/README.md) — the FastAPI application backend, SQLite storage,
  migrations, and development fixtures.
- [`inference/`](inference/README.md) — the private Whisper inference service.

Each service owns its environment and run instructions. The application does
not start, migrate, or invoke the inference service implicitly.

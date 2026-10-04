# inference

Whisper transcription server on top of a **stock** vLLM engine
(`vllm.v1.engine.async_llm.AsyncLLM`). vLLM is used as a library; the HTTP
layer lives in `server.py`, so the API can change without patching vLLM.

The caller must send audio that is ready for Whisper: WAV (or anything
`soundfile` reads), mono, 16 kHz, at most 30 s. Nothing is resampled or
chunked here; anything else is rejected with HTTP 400.

## Run

```bash
.venv/bin/python server.py                 # openai/whisper-tiny on :8000
.venv/bin/python server.py --help          # all vLLM engine args are accepted
```

Defaults are tuned for CPU: `--gpu-memory-utilization 0.4` (on the CPU
backend this is the share of host RAM reserved for the KV cache).
Set `HF_HUB_OFFLINE=1` to start without network once the model is cached.

## API

`GET /health`: 200 while the engine is alive, 503 once it has died.

`POST /v1/audio/transcriptions` (multipart form)

| field         | default | meaning                         |
|---------------|---------|---------------------------------|
| `file`        |         | audio file                      |
| `language`    | `en`    | Whisper language code           |
| `temperature` | `0.0`   | sampling temperature            |
| `top_logprobs`| none    | return per-token logprobs with up to N alternatives (max 20) |
| `starting_tokens` | none | token id; repeat the field for a list |

Response: `{"text": "..."}`, plus with `top_logprobs`:

```json
"token_logprobs": [
  {"token_id": 708, "logprob": -0.01,
   "top_alternatives": [{"token_id": 708, "logprob": -0.01}, ...]},
  ...
]
```

`top_alternatives` is sorted best first and includes the sampled token, so
it can hold N+1 entries. The trailing end-of-text token is included.

`starting_tokens` forces the start of the decoding: the ids are appended
to the decoder prompt after Whisper's special-token prefix
(`<|startoftranscript|><|lang|><|transcribe|><|notimestamps|>`). They are
context, not sampled, so they appear neither in `text` nor in
`token_logprobs`; sampling continues right after them.

## Environment

`.venv/` and `vllm-src/` are not committed. There is no vLLM wheel for
CPU on PyPI, so vLLM is built from source (`VLLM_TARGET_DEVICE=cpu`) at an
unmodified release tag, currently `v0.28.0`, and installed editable from
`vllm-src/`:

```bash
git clone --branch v0.28.0 https://github.com/vllm-project/vllm.git vllm-src
python3.10 -m venv .venv && source .venv/bin/activate
cd vllm-src
pip install -r requirements/cpu.txt --extra-index-url https://download.pytorch.org/whl/cpu
pip install cmake ninja setuptools-rust setuptools-scm wheel jinja2
CC=gcc-12 CXX=g++-12 MAX_JOBS=2 VLLM_TARGET_DEVICE=cpu \
    pip install -e . --no-build-isolation
pip install soundfile av
```

Build needs gcc/g++ >= 12.3 and `libnuma-dev`; keep `MAX_JOBS` low on
machines with little RAM.

To upgrade vLLM: check out the new tag in `vllm-src/`, rebuild, and run a
test transcription; the server relies only on vLLM's public engine API.

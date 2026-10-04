"""Whisper transcription server built on the vLLM Python API.

Uses a stock vLLM engine (`AsyncLLM`) and owns the HTTP layer, so the
request/response format is ours to define. Expects short clips
(<= 30 s, 16 kHz, mono) prepared by the calling backend: no resampling
or chunking is done here.
"""

import io
import os
import uuid
from contextlib import asynccontextmanager

# Same as `vllm serve`: forked engine workers hang with Whisper.
os.environ.setdefault("VLLM_WORKER_MULTIPROC_METHOD", "spawn")

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel

from vllm import SamplingParams
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.exceptions import VLLMClientError
from vllm.outputs import CompletionOutput
from vllm.sampling_params import RequestOutputKind
from vllm.usage.usage_lib import UsageContext
from vllm.utils.argparse_utils import FlexibleArgumentParser
from vllm.v1.engine.async_llm import AsyncLLM

SAMPLE_RATE = 16000
MAX_AUDIO_SECONDS = 30.0


class TokenAlternative(BaseModel):
    token_id: int
    logprob: float


class TokenLogprob(BaseModel):
    token_id: int
    """The token id that was actually sampled at this position."""
    logprob: float
    top_alternatives: list[TokenAlternative]
    """Most likely tokens at this position, best first; includes the
    sampled token."""


class TranscriptionResponse(BaseModel):
    text: str
    token_logprobs: list[TokenLogprob] | None = None


def load_audio(data: bytes) -> np.ndarray:
    try:
        audio, sample_rate = sf.read(io.BytesIO(data), dtype="float32")
    except Exception as e:
        raise HTTPException(400, f"Cannot decode audio: {e}") from e
    if audio.ndim != 1:
        raise HTTPException(400, f"Audio must be mono, got {audio.shape[1]} channels")
    if sample_rate != SAMPLE_RATE:
        raise HTTPException(
            400, f"Audio must be {SAMPLE_RATE} Hz, got {sample_rate} Hz"
        )
    duration = len(audio) / SAMPLE_RATE
    if duration > MAX_AUDIO_SECONDS:
        raise HTTPException(
            400, f"Audio must be at most {MAX_AUDIO_SECONDS} s, got {duration:.2f} s"
        )
    return audio


def token_logprobs(completion: CompletionOutput) -> list[TokenLogprob]:
    result = []
    for token_id, position in zip(completion.token_ids, completion.logprobs):
        alternatives = sorted(
            position.items(), key=lambda item: item[1].logprob, reverse=True
        )
        result.append(
            TokenLogprob(
                token_id=token_id,
                logprob=position[token_id].logprob,
                top_alternatives=[
                    TokenAlternative(token_id=alt_id, logprob=alt.logprob)
                    for alt_id, alt in alternatives
                ],
            )
        )
    return result


def decoder_prefix(engine: AsyncLLM, language: str) -> list[int]:
    """Whisper's special-token decoder prefix, as token ids."""
    tokenizer = engine.get_tokenizer()
    tokens = [
        "<|startoftranscript|>",
        f"<|{language}|>",
        "<|transcribe|>",
        "<|notimestamps|>",
    ]
    ids = tokenizer.convert_tokens_to_ids(tokens)
    if any(i is None or i == tokenizer.unk_token_id for i in ids):
        raise HTTPException(400, f"Unsupported language: {language!r}")
    return ids


def create_app(engine_args: AsyncEngineArgs) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = AsyncLLM.from_engine_args(
            engine_args, usage_context=UsageContext.OPENAI_API_SERVER
        )
        app.state.engine = engine
        try:
            yield
        finally:
            engine.shutdown()

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    async def health() -> Response:
        """200 if the engine is alive, 503 if it has died."""
        try:
            await app.state.engine.check_health()
        except Exception:
            return Response(status_code=503)
        return Response(status_code=200)

    @app.post(
        "/v1/audio/transcriptions",
        response_model=TranscriptionResponse,
        response_model_exclude_none=True,
    )
    async def transcribe(
        file: UploadFile,
        language: str = Form("en"),
        temperature: float = Form(0.0),
        top_logprobs: int | None = Form(None, ge=0),
        starting_tokens: list[int] | None = Form(None),
    ) -> TranscriptionResponse:
        engine: AsyncLLM = app.state.engine
        audio = load_audio(await file.read())
        prompt_ids = decoder_prefix(engine, language)
        if starting_tokens:
            # Forced as decoder context, not sampled: they never appear in
            # the output text or token_logprobs.
            vocab_size = len(engine.get_tokenizer())
            if not all(0 <= t < vocab_size for t in starting_tokens):
                raise HTTPException(
                    400, f"starting_tokens must be in [0, {vocab_size})"
                )
            prompt_ids = prompt_ids + starting_tokens
        max_tokens = engine.model_config.max_model_len - len(prompt_ids)
        if max_tokens < 1:
            raise HTTPException(400, "starting_tokens is too long")

        prompt = {
            "encoder_prompt": {
                "prompt": "",  # Whisper has no text encoder prompt.
                "multi_modal_data": {"audio": (audio, SAMPLE_RATE)},
            },
            "decoder_prompt": {"prompt_token_ids": prompt_ids},
        }
        sampling_params = SamplingParams(
            temperature=temperature,
            logprobs=top_logprobs,
            max_tokens=max_tokens,
            output_kind=RequestOutputKind.FINAL_ONLY,
        )

        final = None
        try:
            async for output in engine.generate(
                prompt, sampling_params, request_id=f"transcribe-{uuid.uuid4()}"
            ):
                final = output
        except (VLLMClientError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        assert final is not None and final.finished

        completion = final.outputs[0]
        return TranscriptionResponse(
            text=completion.text.strip(),
            token_logprobs=(
                token_logprobs(completion) if top_logprobs is not None else None
            ),
        )

    return app


def main() -> None:
    parser = FlexibleArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser = AsyncEngineArgs.add_cli_args(parser)
    # CPU-friendly defaults; on the CPU backend --gpu-memory-utilization
    # controls how much host RAM is reserved for the KV cache.
    parser.set_defaults(model="openai/whisper-tiny", gpu_memory_utilization=0.4)
    args = parser.parse_args()

    app = create_app(AsyncEngineArgs.from_cli_args(args))
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

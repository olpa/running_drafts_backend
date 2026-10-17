# WebM/Opus assembly evidence

Backend issue #7 is verified from real-browser fixtures captured by
`running_drafts_product` commit
[`566520e1d0dfc4d74492686195c7670b457f9fbd`](https://github.com/olpa/running_drafts_product/commit/566520e1d0dfc4d74492686195c7670b457f9fbd).
The backend copy is self-contained under `app/tests/fixtures/mediarecorder/`.
`provenance.json` records the source repository, commit, path, and SHA-256 of
every copied file.

## Captures and results

| Case | Browser | Frames | Joined bytes | Joined SHA-256 | Decoded samples / duration |
| --- | --- | ---: | ---: | --- | ---: |
| completed | Chromium 153.0.8010.12 | 3 | 323187 | `483b12fb040f75cf4f229fdea74c4889ee4348ca99f708a8950db493c7d55a39` | 1031040 / 21.480 s |
| interrupted | Chromium 153.0.8010.12 | 2 | 166927 | `8532ef70a4725da2ffa4c20a7f637311682f5a1e1df578d859c55a517000e3cb` | 550080 / 11.460 s |
| completed | Firefox 155.0 | 3 | 13845 | `9118422a21c40743f8ab738e464659ed17dd49343ab4c1ffba14bdab793e83df` | 1056648 / 22.0135 s |
| interrupted | Firefox 155.0 | 2 | 7363 | `b731db86d8809ef263a43e84161b4cf56c9f0c0fff31c2d12f308436c2ce339e` | 552648 / 11.5135 s |

Each capture used one continuous MediaRecorder with a 10-second timeslice. A
forced main-thread delay moved the first event past 10.4 seconds, and the last
event in every case is a shorter final tail. The interrupted fixtures contain
only frames acknowledged before the simulated page loss.

The verifier reads frames by their zero-based sequence, concatenates their bytes
without parsing or modifying the container, compares the result byte-for-byte
with the product evidence, probes the Opus stream, and decodes the complete
assembly to mono 48 kHz signed 16-bit PCM. It never requires an individual later
frame to decode.

The baseline decoder was FFmpeg/FFprobe 6.1.1-3ubuntu5. The decoded PCM SHA-256
and sample count in `verification.json` are the content oracle: omitted,
duplicated, reordered, or changed audio cannot pass on container timing alone.
The script additionally proves that reversed frames fail to open and that
omitted, duplicated, and mixed-recorder candidates do not match any approved
byte or PCM fingerprint. Both mixed-recorder controls happen to decode, which
is deliberate evidence that decodability by itself must never authorize joining
different recorder runs.

## Continuity and tolerances

Decoded duration may differ from requested wall time by at most 0.75 seconds.
This admits Chromium's 20 ms Opus rounding and Firefox's observed roughly
0.5-second queued tail while rejecting a lost or duplicated transport interval.
Exact sample counts and PCM fingerprints make this tolerance non-cumulative.

Chromium used the deterministic `speech-en.riff` input. Its longest run at an
absolute PCM amplitude of 1 or less is 24,725 samples (515.104 ms) in both
captures; this is source silence, not a frame-boundary gap. The final 100 ms has
RMS amplitude above 30 in both completed and interrupted cases, so the partial
tail contains decoded source content. The exact PCM fingerprint accounts for
content drift, ordering changes, or duplicated speech even when duration stays
the same.

Playwright Firefox's fake microphone produced digital silence in these captures.
Amplitude cannot distinguish a silent gap from a silent duplicate, so the
Firefox-specific evidence is the actual decoded zero-valued PCM length, its
exact fingerprint, the duration tolerance, the byte identity, and the altered-
assembly negative controls. This limitation is explicit rather than inferring
audio continuity from transport timestamps.

Run all executable evidence from `app/`:

```bash
uv run python scripts/verify_webm_assembly.py
uv run pytest tests/test_webm_evidence.py
```

The equivalent manual ordered join and decode for one case are:

```bash
cat tests/fixtures/mediarecorder/chromium-completed/frame-*.webm > /tmp/joined.webm
sha256sum /tmp/joined.webm
ffprobe -v error -show_streams /tmp/joined.webm
ffmpeg -v error -i /tmp/joined.webm -map 0:a:0 -f s16le -acodec pcm_s16le /tmp/joined.pcm
```

## Deterministic empty audio

`app/src/running_drafts/assets/empty-audio.webm` is a 171-byte WebM containing a
mono 48 kHz Opus track and an empty cluster with no audio packets. It opens and
decodes successfully to zero samples. Its SHA-256 is
`2f843b2151bfd44876e137cd5f65c1e8a98b2050cbe054865bf4d86e9292b32f`.

Regenerate it byte-for-byte from `app/` with:

```bash
uv run python scripts/generate_empty_webm.py
```

The generator writes EBML directly so its identity does not vary with FFmpeg
versions. The committed asset is package data and can later be served directly;
it needs no prepared download state. It is synthetic application data, not a
stored transport frame. Sealing a zero-frame asset keeps `frameCount` and
`storedBytes` at zero, so none of the 171 fixture bytes are billed as user audio.

This evidence does not introduce remuxing, transcoding, per-frame decoding, an
assembled persistent copy, inference work, or recorder resumption.

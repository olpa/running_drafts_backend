# Browser media buffering for long Audio Assets

Research date: 2026-11-12

## Question

For an ordinary authenticated `<audio>` URL representing a multi-hour WebM/Opus
snapshot, should Running Drafts prescribe or enforce a buffer such as one, five,
or ten minutes? What should the editor, backend, and browser acceptance tests
require instead?

## Conclusion

Do not set a one-, five-, or ten-minute product buffer. There is no web-platform
consensus for such a value. An ordinary media element chooses the missing byte
range and controls read-ahead using browser, codec, network, playback-rate,
cache, and device heuristics. The backend will separately cap open-ended and
unusually large `206` responses by a configurable byte count as defense in
depth; that cap limits one response, not the browser's aggregate buffer.

The cross-repository requirement should instead be observable:

> Playback is activated on demand and incrementally buffered. Before initial
> playback, and after seeking to a distant position, the client transfers only
> a bounded fraction of a long Audio Asset rather than the whole representation.

The backend should implement correct single-byte-range responses, honor small
finite ranges exactly, bound open-ended or unusually large responses with an
accurate `Content-Range`, and allow the browser to suspend, cancel, or resume
them. The editor should use an ordinary media URL, avoid application-level
full-file fetching, and use `preload="metadata"` after explicit player
activation. Real Chromium and Firefox tests should measure actual response-body
bytes and request churn; a short empirical spike with the release browser
versions should set the cap's launch value and a separate generous aggregate
acceptance ceiling.

This client-transfer requirement is distinct from backend generation work.
Following the research, the product decision is that an uncached request must
not read, remux, or write the complete snapshot before responding. Backend work
must be proportional to the requested output range and use only the source
frames needed to produce that range. Time-to-first-byte, source bytes read,
temporary bytes written, CPU, and memory therefore need separate backend
acceptance evidence.

## Evidence

### The browser, not the application, selects the byte range

The HTML media-resource algorithm defines the required byte range as
implementation-defined. It explicitly permits a user agent to request the whole
resource, an open-ended range, or a bounded range according to codec, network,
and other heuristics. The same standard says `preload` is only a hint and may be
ignored because of connectivity or user preferences. `metadata` asks the browser
to fetch metadata and perhaps initial frames; while playing it also hints that
bandwidth should be treated as scarce. Sources:

- [WHATWG media resource fetching](https://html.spec.whatwg.org/multipage/media.html#concept-media-load-resource)
- [WHATWG `preload`](https://html.spec.whatwg.org/multipage/media.html#attr-media-preload)

The platform therefore gives an ordinary `<audio>` application no portable API
for “buffer exactly N minutes.” The `buffered` API reports what the browser has,
but does not prescribe how much it should fetch. The standard also notes that
time ranges may be estimated from byte streams for variable-bitrate media.
[WHATWG media-element implementation guidance](https://html.spec.whatwg.org/multipage/media.html#best-practices-for-implementers-of-media-elements)

### Current browser policies differ and are much shorter than five minutes

Current Chromium source defines the URL-media forward preload as a tuneable with
a default of 10 seconds and an allowed range of 0–60 seconds. It separately keeps
two seconds behind playback by default. These are implementation settings, not a
web contract.
[Chromium `MultiBufferDataSource`](https://chromium.googlesource.com/chromium/src/+/HEAD/third_party/blink/renderer/platform/media/multi_buffer_data_source.h#312)

Current Firefox source sets ordinary-network media-cache read-ahead to 60 seconds
(30 seconds on cellular) and resumes a suspended connection below 30 seconds
(10 seconds on cellular). Its cache actively suspends and resumes the media
channel. These also are implementation preferences rather than an author-facing
contract.
[Firefox media-cache preferences](https://searchfox.org/firefox-main/source/modules/libpref/init/StaticPrefList.yaml#12678-12700)

Firefox currently starts HTTP media loads with an open-ended `Range: bytes=N-`
request, including at offset zero, and uses a valid partial response to establish
seekability. When a server sends fewer bytes than requested, Firefox treats the
early end as something to reopen when more bytes are needed. A server-side cap
therefore tends to create extra request churn rather than control the browser's
total buffer.
[Firefox `ChannelMediaResource`](https://searchfox.org/firefox-main/source/dom/media/ChannelMediaResource.cpp#1827-1855)
[Firefox short-response recovery](https://searchfox.org/firefox-main/source/dom/media/ChannelMediaResource.cpp#1403-1447)

Chromium likewise describes URL media loading as an in-memory sliding window and
exposes browser-controlled forward/backward buffering and loading deferral.
[Chromium media buffering overview](https://chromium.googlesource.com/website/+/HEAD/site/audio-video/index.md#how-buffering-works)

The closest evidence-based answer to “one, five, or ten minutes?” is therefore:
none is portable, and five or ten minutes is far above the present default
forward read-ahead in both target engines. One minute resembles Firefox's current
ordinary-network default, but it still must not become a product or server
constant.

### Bounded partial fulfillment is legal but needs browser evidence

HTTP byte ranges address representation octets, not media time. RFC 9110 permits
a server to send only a portion of requested ranges first and lets the client ask
for the remainder later. It also recommends range support for efficient partial
retrieval of large representations.
[RFC 9110, Range Requests](https://www.rfc-editor.org/rfc/rfc9110.html#section-14.2)

Thus a server can bound a very large or open-ended requested range and return an
accurate `206 Content-Range`. However:

- a byte cap does not reliably represent a duration for variable-bitrate media;
- the target browsers already suspend or bound their own read-ahead;
- a small server cap adds HTTP request and authorization overhead during normal
  continuous playback; and
- a cap on one response does not cap total bytes, because the client can
  immediately request the next range.

Running Drafts will use a generous configurable cap for open-ended and unusually
large ranges, while honoring small finite ranges exactly. Test the launch value
in both browser families rather than assuming standards compliance implies
smooth native playback.

### Focused follow-up: open-ended ranges and bounded `206` responses

An open-ended request such as `Range: bytes=123456-` describes the desired
remainder of the representation, but it does not prove that all remaining bytes
will cross the network. Chromium's URL media source uses an in-memory sliding
window and explicitly cancels open network connections once buffering is
sufficient. Firefox's media cache suspends and resumes its channel around its
read-ahead thresholds. HTTP/2 and HTTP/3 also provide flow control, so a server
that generates lazily under backpressure need not run arbitrarily far ahead.

Sources:

- [Chromium `MultibufferDataSource::OnBufferingHaveEnough`](https://chromium.googlesource.com/chromium/src/+/HEAD/media/blink/multibuffer_data_source.h#86)
- [Firefox media-cache preferences](https://searchfox.org/firefox-main/source/modules/libpref/init/StaticPrefList.yaml#12678-12700)
- [Firefox `ChannelMediaResource`](https://searchfox.org/firefox-main/source/dom/media/ChannelMediaResource.cpp#1403-1447)
- [HTTP/2 flow control](https://www.rfc-editor.org/rfc/rfc9113.html#section-5.2)
- [HTTP/3 flow control through QUIC](https://www.rfc-editor.org/rfc/rfc9114.html#section-4.1.2)

RFC 9110 explicitly permits a `206 Partial Content` response to return only a
subset of the requested data for server-selected reasons. The response remains
self-describing through `Content-Range`, and the client can request the missing
remainder later. Firefox's short-response path does exactly that: it reopens the
channel when more bytes are needed.

[RFC 9110 `206 Partial Content`](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.3.7)

This makes a generous byte cap on open-ended or unusually large ranges a
reasonable defense-in-depth design for Running Drafts, provided real Chromium
and Firefox observations show smooth playback and bounded request churn. Those
observations are diagnostic rather than an exhaustive compatibility gate. It is
not a time-buffer policy and cannot limit aggregate transfer: a browser may
immediately request the next subrange. Small finite ranges should be honored
exactly. Generation must stop at the returned range end, obey transport
backpressure before that point, and stop immediately on cancellation.

A deliberately shortened `200 OK` is not an alternative. `200` represents the
complete selected representation; partial fulfillment uses `206` with accurate
`Content-Range` and `Content-Length`.

### Product decision: large playback representations are range-only

The standards-based playback backbone assumes native media clients request large
playback representations with byte ranges. `HEAD` remains available for
every snapshot. An un-ranged `GET` may return the complete representation with
`200 OK` only when its total size is at or below the configured playback-response
byte threshold. Above that threshold, the backend returns stable `400` Problem
Details code `range_required` plus `Accept-Ranges: bytes`.

This is an intentional application compatibility boundary, not generic HTTP
media-server behavior. Native `<audio>` does not interpret Problem Details. A
browser that begins a large resource with an un-ranged `GET` will fail playback;
concrete incompatibilities are fixed when observed rather than guarded by an
exhaustive engine/version launch matrix.

Playback supports exactly one byte range per request. Any request containing
multiple ranges is rejected with `416 Range Not Satisfiable`,
`Content-Range: bytes */TOTAL`, and stable Problem Details. The service does not
produce `multipart/byteranges`; target native media clients are expected to use
single ranges.

Single prefix, bounded, open-ended, and suffix forms use their standard HTTP
meaning. A suffix request (`bytes=-N`) is honored exactly only when `N` is at or
below `MAX_PLAYBACK_RESPONSE_BYTES`. An oversized suffix is rejected with `416`,
`Content-Range: bytes */TOTAL`, and stable code `range_too_large`; the server does
not choose a smaller suffix. Start-based open or oversized ranges retain the
separate bounded-`206` behavior from their requested start.

Every immutable snapshot has a strong ETag derived from the API v1 playback
representation rules, Audio Asset identity, `frameCount`, and the ordered stored
Transport-frame SHA-256 hashes. The virtual WebM mapping is deterministic, so
these immutable inputs identify exact output bytes. Validator calculation reads
frame metadata only; it must not generate or hash the complete playback body.

For `If-Range`, a matching strong ETag serves the requested bounded `206`. A
mismatch follows the un-ranged policy: snapshots at or below
`MAX_PLAYBACK_RESPONSE_BYTES` return the complete `200`; larger snapshots return
stable `400 range_required` with `Accept-Ranges: bytes` and the current ETag so
the client can retry. A correct immutable snapshot URL never changes validators;
the mismatch path exists for invalid clients or corrupt cache state.

Snapshot `GET` and `HEAD` support `If-None-Match`. A matching validator returns
`304 Not Modified` with no body. Cache preconditions are evaluated before
`Range`, so a request containing both a matching `If-None-Match` and `Range`
returns `304`; a nonmatching condition proceeds through the normal full or range
contract.

`GET` and `HEAD /api/v1/audio-assets/{audioAssetId}/playback/latest` return a
`307 Temporary Redirect` to the canonical immutable
`/api/v1/audio-assets/{audioAssetId}/playback/snapshots/{frameCount}` URL. The
mutable discovery response uses `Cache-Control: private, no-store`; `307`
preserves `GET` versus `HEAD`. Snapshot `0` is a valid redirect target.

### Bytes are the correct transfer acceptance unit

The product concern is bandwidth and accidental whole-resource transfer, so
response-body bytes are the direct acceptance measure. Minutes are useful only
for describing the playback journey.

The committed recorder fixtures also demonstrate why a universal conversion is
weak. The Chromium completed fixture contains 323,187 bytes for 21.48 seconds,
while the Firefox completed fixture contains 13,845 bytes for 22.01 seconds.
The deterministic input differs, but the roughly 23-fold byte-rate difference is
enough to reject a fixed “minutes equals bytes” assumption.
[Chromium fixture metadata](../../app/tests/fixtures/mediarecorder/chromium-completed/metadata.json)
[Firefox fixture metadata](../../app/tests/fixtures/mediarecorder/firefox-completed/metadata.json)

For diagnostics, tests should record both transferred bytes and the resulting
`audio.buffered` time ranges. Only transferred bytes should be the hard resource
bound.

## Recommended acceptance design

Use a cold browser context and an immutable multi-hour snapshot large enough that
full transfer is unmistakable. Record every request, `Range`, status,
`Content-Range`, response-body byte count, cancellation, and snapshot validator.
Run the same journey in release-target Chromium and Firefox.

1. Before placeholder activation, assert zero playback requests.
2. Activate the player with `preload="metadata"`; wait for finite duration and a
   seekable range; assert transferred bytes are well below the representation
   size.
3. Start at the beginning; wait for `playing` and five seconds of advancing
   `currentTime`; assert the cumulative transferred bytes remain bounded.
4. Seek near the end; wait for `seeked` and resumed time advancement; assert the
   additional bytes remain bounded and the complete representation has still not
   been transferred.
5. Let playback continue across any server-imposed internal chunk boundary to
   ensure no avoidable stall or request loop.

Do not derive a normative ceiling solely from standards: they intentionally leave
fetch size to the browser. First measure the exact target browsers against the
real indexed WebM/Opus representation over the local acceptance server. Then set
one deliberately generous byte ceiling that is:

- above the largest observed activation or seek transfer with safety margin;
- below both 10% of the long fixture and an absolute audio-specific ceiling; and
- recorded beside the fixture bitrate, representation size, browser version, and
  journey phase so later browser-policy changes are diagnosable.

The playback response cap is a separate fixed launch decision:
`MAX_PLAYBACK_RESPONSE_BYTES=1572864` (1.5 MiB). Aggregate bytes per activation
or seek remain an empirical acceptance bound because a browser may issue several
bounded responses. The browser spike sets that aggregate ceiling with safety
margin while still proving that the browser did not fetch a meaningful fraction
of a multi-hour asset.

Playback uses capacity leases independent of frame uploads and original-download
staging, with mandatory launch settings
`MAX_ACCOUNT_CONCURRENT_PLAYBACK_RESPONSES=4` and
`MAX_PROCESS_CONCURRENT_PLAYBACK_RESPONSES=8`. A lease covers generation and
streaming of one response and releases on every completion, rejection,
cancellation, and disconnect path. Acceptance holds eight responses active,
rejects the ninth before reading frame payloads with stable
`429 playback_concurrency_limit_exceeded` and no `Retry-After`, then proves
capacity returns after cleanup.

Account and process saturation have the same public Problem Details: no scope
extension and no `Retry-After`. Structured server logs record whether the
Account or process limit rejected the request for operational diagnosis without
exposing process capacity to clients.

Playback capacity applies only to responses that generate a media body. The API
authenticates, resolves current ownership and snapshot metadata, and evaluates
method, cache preconditions, and range validity first. `404`, `HEAD`, `304`,
`400`, and `416` responses use no playback lease. A valid body-producing `200`
or `206` acquires both Account and process capacity before reading any Transport
frame payload; failure to acquire returns the common `429`.

SQLite may store compact ingestion-derived playback-index state: per-frame
metadata, rolling sums, cumulative virtual offsets, current totals, checkpoints,
and cue inputs needed for deterministic range mapping. This is not
request-derived snapshot state or derivative audio. It is reproducible from
authoritative frame bytes, excluded from Account `storedBytes`, and removed with
the owning Audio Asset. The database must never create a row or blob merely
because a client requested a snapshot.

Required playback-index state is committed atomically with each accepted
Transport frame and its accounting. Once that acknowledgement transaction has
committed, `playback/latest` immediately resolves to the new `frameCount`; there
is no separate playback-ready count or asynchronous indexing lag. The ingestion
path may perform only bounded structural parsing and index maintenance—never
audio decoding, re-encoding, or a scan of the full acknowledged prefix.
If that structural validation or index update fails because the submitted media
is invalid, the API rejects the Transport frame before acknowledgement. Frame
bytes, accounting, acknowledged `frameCount`, and playback-index state all roll
back together; the editor retains responsibility for the unacknowledged bytes.
The backend does not prescribe the editor's retry policy and stores no poison or
terminal marker for a rejected submission. If the frame is submitted again, it
is validated again and may be accepted if that submission is valid.

Audio Assets accepted before this index schema exists must remain playable
without re-upload. Their playback-index metadata is backfilled reproducibly from
the authoritative stored Transport frames during a synchronous, transactional
upgrade migration. The new backend does not serve traffic until the migration
completes. Failure rolls the migration back and leaves the deployment unhealthy;
the migration never deletes recordings. This avoids a background worker, lazy
first-request indexing, and a separate playback-pending state.
If any already-acknowledged historical frame cannot be indexed as valid playback
media, the entire migration fails and rolls back. It must neither skip that frame
nor publish altered/partial playback. Diagnostics identify the Audio Asset and
frame without logging the audio payload.

Every playback network request uses the normal HttpOnly Application Session
cookie and repeats authentication plus current ownership authorization. This
includes `latest`, snapshot `GET`/`HEAD`, and subsequent range requests. Do not
mint signed or bearer playback URLs. Session expiry therefore rejects later
network requests normally; bytes already held in the browser cache remain subject
to the separately accepted private immutable-cache behavior.

Reject a malformed `Range` header or an unsupported range unit with Problem
Details `400 invalid_range`; do not guess, ignore it, or fall back to a full-body
response. Reserve `416` plus `Content-Range: bytes */TOTAL` for syntactically
valid byte-range requests that are unsatisfiable or violate the supported
single-range/oversized-suffix contract.

An Audio Asset has one WebM/Opus track configuration established by its first
acknowledged media. Encoded bitrate may vary, but a later frame that changes the
codec, channel count, sample rate, or other incompatible track parameters is
rejected before acknowledgement with the same atomic rollback guarantees as
other structurally invalid media. Playback does not transcode or create multiple
tracks to accommodate such a change.

Frame validation exposes two stable Problem Details errors before
acknowledgement: `422 invalid_audio_frame` for malformed or unparseable
WebM/Opus, and `409 audio_track_mismatch` for otherwise valid media incompatible
with the Audio Asset's established track configuration. Public responses do not
include low-level parser diagnostics or media content.

For compatible ordered frames from one continuous recorder run, the playback
representation normalizes WebM container timestamps into one continuous timeline
even when source segment timestamps reset, overlap, or contain gaps within that
run. Concatenate acknowledged Opus packets in frame order without inserting
silence and without changing packet bytes. A new `MediaRecorder` run always
creates another Section and Audio Asset, even when its track parameters match.
Exact original download continues to preserve the submitted container bytes and
timestamps.

If stored frame bytes, hashes, or playback-index invariants disagree after
acknowledgement, never skip, guess, repair, or emit a best-effort representation.
Return Problem Details `500 playback_representation_corrupt` when response
headers have not been sent; if streaming has begun, terminate it immediately.
Preserve the original bytes and emit operational diagnostics with Audio Asset and
frame identifiers but no media content.

Playback does not recompute stored frame SHA-256 hashes on each response. It
trusts media after transactional acceptance; the corruption behavior applies to
inconsistencies encountered naturally while resolving or generating playback.
Background integrity scrubbing is outside this ticket.

Playback responses use `Content-Type: audio/webm; codecs=opus` and omit
`Content-Disposition` entirely. They expose neither an attachment disposition
nor a filename; those remain exclusive to the original exact-byte download
endpoint.

Project deletion receives no special coordination with active playback. Once
deleted, new playback requests resolve as privacy-preserving `404`. A response
already streaming during the rare race may either finish or terminate according
to ordinary database/request timing; neither outcome is guaranteed. Do not add a
deletion gate, wait for playback leases, retain tombstones, or build deferred
cleanup for this case.

The initial backend retains no generated playback bytes between requests: no
snapshot BLOBs, temporary derivative files, or cross-request memory cache.
Persist only the compact ingestion-derived index and generate each requested
body range on demand. Browser private immutable caching supplies reuse. A bounded
server cache may be considered later from measurements without changing the API.

Browser-specific compatibility is not a release gate for the backend ticket.
Implement the standards-based ordinary media URL, byte-range, authentication,
and virtual-WebM backbone with focused contract tests. Browser observations may
inform later fixes, but do not claim or require an exhaustive engine/version
support matrix for launch.

The virtual, deterministic, range-proportional architecture is a hard boundary.
If implementation cannot satisfy it, stop the ticket and return to explicit
requirements grilling. Never silently redesign, materialize full snapshots, add
full-prefix processing, or relax the contract without that product decision.

## Estimating duration from WebM/Opus bytes

WebM has no universal time-to-size ratio. Opus is variable bitrate, and the
MediaRecorder specification lets the user agent choose a reasonable target when
the application does not supply `audioBitsPerSecond`. The current editor supplies
only `mimeType`, so browser choice and source content determine the result.

Sources:

- [MediaStream Recording bitrate selection](https://www.w3.org/TR/mediastream-recording/#mediarecorder-constructor)
- [Opus bitrate guidance](https://www.rfc-editor.org/rfc/rfc6716.html#section-2.1.1)
- [Current editor recorder construction](../../../running_drafts_editor/web/src/recording/capture.ts)

Ignoring the relatively small WebM container overhead, the conversion is:

`MiB per minute = kbit/s * 1000 * 60 / 8 / 1,048,576`

| Encoded bitrate | MiB/min | MiB/5 min | MiB/10 min |
| ---: | ---: | ---: | ---: |
| 32 kbit/s | 0.23 | 1.14 | 2.29 |
| 64 kbit/s | 0.46 | 2.29 | 4.58 |
| 96 kbit/s | 0.69 | 3.43 | 6.87 |
| 120 kbit/s | 0.86 | 4.29 | 8.58 |
| 128 kbit/s | 0.92 | 4.58 | 9.16 |
| 256 kbit/s | 1.83 | 9.16 | 18.31 |
| 510 kbit/s | 3.65 | 18.24 | 36.48 |

The committed real Chromium fixture is 323,187 bytes over 21.507 seconds:
approximately 120.2 kbit/s including WebM overhead, or 0.86 MiB/minute. The
Firefox fixture is only about 5 kbit/s for its deterministic source, illustrating
why duration cannot be inferred reliably from bytes under VBR. Chromium source
currently caps explicitly selected or automatically allocated Opus bitrate at
128 kbit/s, but that implementation detail is not a cross-browser contract.

[Chromium MediaRecorder bitrate allocation](https://chromium.googlesource.com/chromium/src/+/HEAD/third_party/blink/renderer/modules/mediarecorder/media_recorder.cc)

For the current Chromium evidence, a 4 MiB response represents roughly 4.7
minutes and an 8 MiB response roughly 9.3 minutes. Both exceed current browser
read-ahead targets by a wide margin. The response cap remains a byte resource
limit rather than a promised duration.

### Product decision: explicit voice capture target

Whisper does not define a WebM/Opus bitrate. The project inference boundary
accepts decoded mono 16 kHz audio; compressed browser audio is decoded,
downmixed, and resampled before inference. Official OpenAI file transcription
also accepts several compressed containers, including WebM, without defining a
single source bitrate.

Sources:

- [Project Whisper inference input](../../inference/README.md)
- [OpenAI file transcription formats](https://developers.openai.com/api/docs/guides/speech-to-text)

The editor will instead use the Opus codec's full-band speech guidance as the
capture policy: explicitly request `audioBitsPerSecond: 32_000`, within Opus's
28–40 kbit/s speech sweet spot, while retaining variable bitrate and the browser
produced WebM/Opus stream. This is a target, not a promise of exact output size.
The backend remains bitrate-agnostic and continues to accept any otherwise valid
WebM/Opus Audio Asset.

At 32 kbit/s, encoded audio is approximately 0.229 MiB/minute before WebM
overhead: about 1.14 MiB for five minutes and 2.29 MiB for ten minutes. Playback
response sizing uses this expected voice target while remaining a byte limit.
The mandatory launch setting is `MAX_PLAYBACK_RESPONSE_BYTES=1572864` (1.5 MiB),
which provides about 31% margin above five minutes of 32 kbit/s payload for WebM
overhead and bitrate variation.

## Backend implementation measurement: no derivative materialization

HTTP range behavior only bounds bytes sent to the client. A backend can still
read all source frames and write a full derivative before returning the first
small range. Conversely, a virtual byte-addressable representation could serve a
range without a complete derivative file.

The technical spike for backend #33 should therefore measure independently:

- time to response headers and first body byte versus snapshot duration;
- source bytes read before first body byte;
- temporary/cache bytes written before first body byte;
- CPU time and peak memory;
- duplicate work for concurrent requests for the same snapshot; and
- byte stability and regeneration behavior after restart.

No browser or HTTP source establishes the numeric bound for this server-side
work. Implementation must prove the chosen range-addressable representation
satisfies the product decision without materializing the complete derivative.

## Remaining empirical questions

- What request shapes and byte totals do current Chromium and Firefox versions
  produce against the exact indexed WebM/Opus representation?
- Does authenticated private caching combine partial responses as expected in
  both browsers?
- Does the local high-throughput test environment cause overshoot before browser
  suspension, and if so is traffic shaping needed for stable acceptance?
- Does the implementation's deterministic virtual mapping touch only the indexed
  source regions needed for each output byte range while remaining seekable?

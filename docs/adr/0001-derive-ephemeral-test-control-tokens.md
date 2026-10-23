---
status: accepted
---

# Derive ephemeral test-control tokens from a startup challenge

Online fixture controls use two configured secrets instead of accepting a long-lived configured bearer token directly. A trusted provisioning client authenticates to a challenge endpoint with the bootstrap credential, combines the process-lifetime random challenge with the separate derivation key using a versioned HMAC-SHA-256 contract, and gives consumers only the derived instance token; this limits a leaked consumer credential to one backend process lifetime while keeping configured secrets out of test artifacts.

The controls are environment-independent and exist only when both configured secrets are nonempty. This deliberately permits controlled production use: safety comes from explicit credential provisioning, a fixed fixture Account ownership boundary, and transactional cascade cleanup rather than from `APP_ENV`.

The product sandbox manager owns derivation for local sandboxes and writes only the ready-to-send `X-Test-Control` header to its protected credentials file. Application Sessions issued through fixture creation remain ordinary opaque, digest-persisted browser sessions; the derived control token is never an alternate application-authentication path.

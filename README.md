# modelman - self-hosted small model services

A small, boring delivery pipeline for small self-hosted models: one image per
service, built by CI, pulled by the deployment host, health-gated on deploy.

The first and currently only service is **OCR** (PP-OCR v5/v6 via MNN), which
replaces the hand-mounted binary deployment that lived in the `kuba` repository
with a versioned image.

## Layout

```
modelman/
├── Cargo.toml                  # cargo workspace
├── Makefile                    # build/test/image entry points
├── registry/                   # model registry: which model each service runs
├── services/
│   └── ocr/                    # PP-OCR detection + recognition service
│       ├── models/             # MNN model files and dictionaries
│       ├── src/                # service code
│       ├── tests/fixtures/     # contract-test corpus and baselines
│       └── Dockerfile
└── docs/
    ├── architecture.md         # service conventions and directory roles
    ├── adding-a-service.md     # checklist for the next service
    ├── ocr-model-selection.md  # measured tier comparison
    └── deployment.md           # image, registry and host-side deployment
```

## Quick start

```bash
make build     # cargo build --release
make test      # unit + contract + http tests (runs real inference)
make run       # serve on 0.0.0.0:8080 with models/
make image     # docker image modelman-ocr:local
```

Smoke test the running service:

```bash
curl -s http://127.0.0.1:8080/healthz
curl -s -F image=@services/ocr/tests/fixtures/case_05.png \
     http://127.0.0.1:8080/ocr
```

## HTTP surface

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health`, `/healthz` | no | liveness with loaded models and uptime |
| GET | `/livez` | no | process is up |
| GET | `/readyz` | no | default tier resident, safe for traffic |
| GET | `/version` | no | version, commit, build time, effective config |
| GET | `/models` | no | shipped tiers, loaded state, load errors |
| GET | `/metrics` | yes | Prometheus text exposition |
| POST | `/ocr` | yes | recognise one image |
| POST | `/ocr/batch` | yes | recognise several images in one request |

`/ocr` accepts `multipart/form-data` with an `image` field plus optional
`model` and `backend` query parameters, and returns the response shape the
predecessor service already used:

```json
{
  "success": true,
  "results": [{ "text": "...", "confidence": 0.93, "bbox": { "left": 0, "top": 0, "width": 150, "height": 33 } }],
  "time_ms": 28.4,
  "model": "v6small",
  "backend": "cpu",
  "error": null
}
```

Auth is off unless `AUTH_TOKEN` is set; when set, `/ocr`, `/ocr/batch` and
`/metrics` require `X-Auth-Token: <token>` or `Authorization: Bearer <token>`.
Health endpoints deliberately stay open so a container can be probed before
credentials exist.

## Model tiers

| Tier | Notes |
|---|---|
| `v6small` | **default.** Best accuracy per millisecond on the shipped corpus |
| `v6tiny` | ~5x faster and ~60% less memory, but misses roughly 40% of the lines `v6small` reads |
| `v5` | previous generation, kept for output compatibility |

Measurements and the reason PP-OCRv6 medium is not shipped are in
`docs/ocr-model-selection.md`.

## Delivery

`modelman` never deploys itself. A tag push builds and publishes an image to
GHCR; the `cops` repository pins that image tag and deploys it. See
`docs/deployment.md`.

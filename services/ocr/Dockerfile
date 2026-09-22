# Runtime image for the OCR service.
#
# Single stage on purpose. The Rust binary is built on the CI runner rather
# than inside the image for two reasons: the MNN prebuilt archive is fetched by
# the ocr-rs build script over the network (cheaper to cache on the runner than
# in a layer), and linking inside a container has previously dropped MNN's
# static initialisers. `make build` and the release workflow both build first
# and then invoke this Dockerfile.
#
# Build from the repository root so the model directory is in context:
#   cargo build --release
#   docker build -f services/ocr/Dockerfile -t modelman-ocr:local .

FROM debian:bookworm-slim

# libstdc++6 is the only non-base shared library the binary needs; MNN is
# linked statically and the Vulkan backend is compiled out, so no
# libvulkan/OpenCL/CUDA runtime is required.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# All tiers ship in the image: they total under 40 MB and that removes a
# rebuild from the model-switching loop. PRELOAD_MODELS decides what is held in
# memory at runtime.
COPY services/ocr/models /app/models
COPY target/release/ocr-service /app/ocr-service

ENV MODELS_DIR=/app/models \
    LISTEN_ADDR=0.0.0.0:8080 \
    DEFAULT_MODEL=v6small \
    PRELOAD_MODELS=v6small \
    CONF_THRESHOLD=0.3 \
    MAX_CONCURRENCY=2 \
    OMP_NUM_THREADS=2 \
    RUST_LOG=ocr_service=info

EXPOSE 8080

# The binary probes itself, so the image needs no curl/wget.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/app/ocr-service", "--healthcheck"]

CMD ["/app/ocr-service"]

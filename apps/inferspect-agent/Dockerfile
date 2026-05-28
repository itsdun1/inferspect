# syntax=docker/dockerfile:1.6
#
# inferspect-agent — privileged eBPF observer.
#
# Two-stage build:
#   1. clang/llvm + libbpf headers compile bpf/ssl_uprobe.c → ssl_uprobe.o
#   2. golang builds the user-space binary
#
# Runtime image is debian:bookworm-slim with libssl3 (so the uprobe target
# exists when running against in-container apps) and the BPF object copied in.
#
# Container MUST run --privileged --pid=host --network=host on Linux. Docker
# Desktop's Linux VM on macOS is sufficient for local dev.

# ─── Stage 1: compile BPF ──────────────────────────────────────────────
FROM debian:bookworm-slim AS bpfbuild
RUN apt-get update && apt-get install -y --no-install-recommends \
    clang llvm libbpf-dev linux-headers-amd64 make \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY bpf/ ./bpf/
RUN mkdir -p /out && clang -O2 -g -Wall -Werror -target bpf \
    -D__TARGET_ARCH_x86 \
    -I/usr/include/x86_64-linux-gnu \
    -c bpf/ssl_uprobe.c -o /out/ssl_uprobe.o

# ─── Stage 2: build Go user-space ──────────────────────────────────────
FROM golang:1.22-bookworm AS gobuild
WORKDIR /src
COPY go.mod ./
# Don't COPY go.sum — we don't ship one; it's generated in-image so the
# repo stays free of network-state. ``go mod download`` writes go.sum from
# the resolved module graph.
COPY . .
ENV CGO_ENABLED=0 GOFLAGS=-mod=mod
RUN mkdir -p /out && go mod tidy && go build -trimpath -ldflags "-s -w" -o /out/inferspect-agent ./cmd/agent

# ─── Stage 3: runtime ──────────────────────────────────────────────────
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=gobuild /out/inferspect-agent /usr/local/bin/inferspect-agent
COPY --from=bpfbuild /out/ssl_uprobe.o /usr/local/share/inferspect/ssl_uprobe.o
RUN mkdir -p /var/lib/inferspect

ENTRYPOINT ["/usr/local/bin/inferspect-agent"]

#!/usr/bin/env bash
# Build inferspect-agent natively on a Linux host.
#
# Detects host arch (uname -m), picks the right vmlinux shim via
# -D__TARGET_ARCH_*, compiles the BPF object, then builds the Go binary.
# Output lands in ./out/.
set -euo pipefail

cd "$(dirname "$0")/.."

OUT=./out
mkdir -p "$OUT"

ARCH=$(uname -m)
case "$ARCH" in
  x86_64|amd64)
    BPF_ARCH_DEF="-D__TARGET_ARCH_x86"
    INC="/usr/include/x86_64-linux-gnu"
    ;;
  aarch64|arm64)
    BPF_ARCH_DEF="-D__TARGET_ARCH_arm64"
    INC="/usr/include/aarch64-linux-gnu"
    ;;
  *)
    echo "unsupported arch: $ARCH" >&2
    exit 1
    ;;
esac

echo "building BPF for $ARCH ($BPF_ARCH_DEF)"
clang -O2 -g -Wall -Werror -target bpf \
    "$BPF_ARCH_DEF" \
    -I"$INC" \
    -c bpf/ssl_uprobe.c -o "$OUT/ssl_uprobe.o"

echo "building Go user-space binary"
CGO_ENABLED=0 go mod tidy
CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o "$OUT/inferspect-agent" ./cmd/agent

echo ""
echo "built:"
ls -la "$OUT"/

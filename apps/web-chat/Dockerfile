# Multi-stage Next.js 16 build for the chat surface (end-user product).
# Builds against the workspace so @ollive/web-shared resolves from local source.

FROM node:20-bookworm-slim AS deps
WORKDIR /workspace
# Copy the npm workspace manifest + both packages' manifests so the install
# layer only invalidates when manifests change.
COPY package.json package-lock.json* ./
COPY apps/web-chat/package.json ./apps/web-chat/package.json
COPY apps/web-shared/package.json ./apps/web-shared/package.json
RUN npm install --no-audit --no-fund

FROM node:20-bookworm-slim AS builder
WORKDIR /workspace
COPY --from=deps /workspace/node_modules ./node_modules
# Now bring in source for both packages.
COPY package.json package-lock.json* ./
COPY apps/web-shared ./apps/web-shared
COPY apps/web-chat ./apps/web-chat
# Build-time public env (baked into the JS bundle).
ARG NEXT_PUBLIC_CHAT_API_URL=/api/chat
ENV NEXT_PUBLIC_CHAT_API_URL=$NEXT_PUBLIC_CHAT_API_URL \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /workspace/apps/web-chat
RUN npm run build

FROM node:20-bookworm-slim AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
RUN groupadd --gid 1001 nodejs && useradd --uid 1001 --gid nodejs --shell /bin/bash --create-home nextjs

# Standalone output rooted at the workspace. The standalone bundle ships
# apps/web-chat/server.js + the symlinked workspace packages it needs.
COPY --from=builder --chown=nextjs:nodejs /workspace/apps/web-chat/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /workspace/apps/web-chat/.next/static ./apps/web-chat/.next/static
COPY --from=builder --chown=nextjs:nodejs /workspace/apps/web-chat/public ./apps/web-chat/public

USER nextjs
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app/apps/web-chat
CMD ["node", "server.js"]

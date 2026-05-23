# Multi-stage Next.js 16 build for the operator console (web-insights).
# Builds against the workspace so @ollive/web-shared resolves locally.

FROM node:20-bookworm-slim AS deps
WORKDIR /workspace
COPY package.json package-lock.json* ./
COPY apps/web-insights/package.json ./apps/web-insights/package.json
COPY apps/web-shared/package.json ./apps/web-shared/package.json
RUN npm install --no-audit --no-fund

FROM node:20-bookworm-slim AS builder
WORKDIR /workspace
COPY --from=deps /workspace/node_modules ./node_modules
COPY package.json package-lock.json* ./
COPY apps/web-shared ./apps/web-shared
COPY apps/web-insights ./apps/web-insights
ARG NEXT_PUBLIC_INSIGHTS_API_URL=/api/insights
ENV NEXT_PUBLIC_INSIGHTS_API_URL=$NEXT_PUBLIC_INSIGHTS_API_URL \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /workspace/apps/web-insights
RUN npm run build

FROM node:20-bookworm-slim AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
RUN groupadd --gid 1001 nodejs && useradd --uid 1001 --gid nodejs --shell /bin/bash --create-home nextjs

COPY --from=builder --chown=nextjs:nodejs /workspace/apps/web-insights/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /workspace/apps/web-insights/.next/static ./apps/web-insights/.next/static
COPY --from=builder --chown=nextjs:nodejs /workspace/apps/web-insights/public ./apps/web-insights/public

USER nextjs
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app/apps/web-insights
CMD ["node", "server.js"]

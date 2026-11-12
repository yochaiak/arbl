FROM node:22-bookworm-slim
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@10.12.1 --activate
COPY apps/web/package.json apps/web/pnpm-lock.yaml /app/
RUN pnpm install --frozen-lockfile
COPY apps/web /app/
ARG NEXT_PUBLIC_API_URL
RUN test -n "$NEXT_PUBLIC_API_URL"
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
RUN pnpm build
USER node
EXPOSE 3000
CMD ["node_modules/.bin/next", "start", "--hostname", "0.0.0.0", "--port", "3000"]

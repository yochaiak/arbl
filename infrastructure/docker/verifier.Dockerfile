FROM node:22-bookworm-slim
WORKDIR /app
ENV NODE_ENV=production
COPY apps/verifier/package.json apps/verifier/package-lock.json /app/
RUN npm ci --omit=dev --ignore-scripts
COPY apps/verifier/server.js /app/server.js
USER node
CMD ["node", "server.js"]

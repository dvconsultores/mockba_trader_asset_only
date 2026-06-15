# ── Stage 1: Build React app ──────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /build

COPY package.json package-lock.json* ./
RUN npm install --frozen-lockfile 2>/dev/null || npm install

COPY . .
RUN npm run build

# ── Stage 2: Serve with nginx ──────────────────────────────
FROM nginx:alpine

COPY --from=builder /build/dist /usr/share/nginx/html

# Nginx config: SPA fallback + SSE-aware /api proxy
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]

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

# Nginx config: SPA fallback + proxy /api to backend
RUN echo 'server { \
    listen 80; \
    root /usr/share/nginx/html; \
    index index.html; \
    location / { try_files $uri /index.html; } \
    location /api/ { proxy_pass http://dashboard:8080; proxy_buffering off; proxy_set_header Host $host; } \
}' > /etc/nginx/conf.d/default.conf

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]

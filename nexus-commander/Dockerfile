# ═══════════════════════════════════════════════════════════════
# NEXUS Commander — High-Performance Frontend Serving
# ═══════════════════════════════════════════════════════════════

# Stage 1: Build the SPA
FROM node:20-alpine AS builder

WORKDIR /app

# Install dependencies first (leverage Docker layer caching)
COPY package*.json ./
RUN npm ci

# Copy the rest of the application
COPY . .

# Pass build arguments and run Vite build
# Provide sensible defaults or leverage them from docker-compose
ARG VITE_API_URL=http://localhost:8000
ARG VITE_WS_URL=ws://localhost:8000/ws
ENV VITE_API_URL=$VITE_API_URL
ENV VITE_WS_URL=$VITE_WS_URL

RUN npm run build

# Stage 2: Serve with NGINX Alpine
FROM nginx:alpine

# Copy built assets from builder
COPY --from=builder /app/dist /usr/share/nginx/html

# Replace default Nginx configuration for SPA routing
RUN echo "server { \
    listen 80; \
    server_name localhost; \
    location / { \
        root /usr/share/nginx/html; \
        index index.html index.htm; \
        try_files \$uri \$uri/ /index.html; \
    } \
}" > /etc/nginx/conf.d/default.conf

# Expose port (internal)
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]

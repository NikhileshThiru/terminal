# syntax=docker/dockerfile:1.7
# Production image: build the static bundle, serve it with nginx.
# VITE_API_URL is baked in at build time (it's read by the browser, so it must
# be the API address *as the browser sees it*, e.g. http://localhost:8000).

FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG VITE_API_URL=http://localhost:8000
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 5173
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1:5173/ >/dev/null || exit 1

# Frontend container: build the Angular app to static files, then serve them
# with nginx. nginx also reverse-proxies /api/* to Kong, so the browser only
# ever talks to one origin (no CORS) — see DEPLOYMENT.md §1 and §5.
#
# Build context is this directory (frontend/), NOT the repo root:
#   docker build -t frontend ./frontend
# or via compose with `context: ./frontend`.

# --- build stage: compile Angular to static files ---
FROM node:20-alpine AS build
WORKDIR /app

# install deps first so this layer caches unless package files change
COPY package*.json ./
RUN npm ci

# build the app (outputs to dist/frontend/browser per angular.json)
COPY . .
RUN npm run build

# --- serve stage: nginx serves the built files + proxies /api to Kong ---
FROM nginx:1.27-alpine
COPY --from=build /app/dist/frontend/browser /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80

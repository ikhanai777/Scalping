# --- build the web UI ---
FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- backend ---
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 SCALPER_SERVER__HOST=0.0.0.0
COPY backend/ backend/
RUN pip install --no-cache-dir "./backend[ml]"
COPY config/ config/
COPY --from=ui /ui/dist frontend/dist
VOLUME /app/data
EXPOSE 8000
CMD ["scalper", "serve"]

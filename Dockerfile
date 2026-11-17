FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY launch.py ./
COPY --from=frontend /build/dist ./frontend/dist
RUN useradd --create-home studio
USER studio
ENV PORT=10000 RENDER=true COOKIE_SECURE=1
CMD ["python", "launch.py"]

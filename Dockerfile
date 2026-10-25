# ---- stage 1: build the React UI -------------------------------------------
FROM node:20-alpine AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
# vite.config.js writes to ../backend/static, so emit into /ui-out instead for Docker
RUN npx vite build --outDir /ui-out --emptyOutDir

# ---- stage 2: FastAPI + static UI -------------------------------------------
FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=ui /ui-out ./static
EXPOSE 8000
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]

FROM node:22-bookworm-slim AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build && test -f dist/index.html

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=azure FRONTEND_DIST=/app/frontend/dist
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt \
    && groupadd --gid 10001 retail && useradd --uid 10001 --gid retail --no-create-home retail
COPY backend/retail/ /app/retail/
COPY --from=frontend-build /build/frontend/dist/ /app/frontend/dist/
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "retail.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]

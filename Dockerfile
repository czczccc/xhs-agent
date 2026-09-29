# xhs-agent 生产镜像（东京15刀 VPS）
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 中文字体（封面渲染用）+ curl（健康检查用）
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[pg]"

EXPOSE 8000

CMD ["uvicorn", "xhs_agent.web.app:app", "--host", "0.0.0.0", "--port", "8000"]

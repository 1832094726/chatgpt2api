ARG BUILDPLATFORM
ARG TARGETPLATFORM
ARG TARGETARCH

FROM --platform=$BUILDPLATFORM node:22-alpine AS web-build

WORKDIR /app/web

COPY web/package.json web/bun.lock ./
RUN npm install

COPY VERSION /app/VERSION
COPY CHANGELOG.md /app/CHANGELOG.md
COPY web ./
RUN NEXT_PUBLIC_APP_VERSION="$(cat /app/VERSION)" npm run build


FROM --platform=$TARGETPLATFORM python:3.13-slim AS app

ARG TARGETPLATFORM
ARG TARGETARCH
ARG APT_MIRROR=
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX=${PIP_INDEX_URL}

WORKDIR /app

# 安装系统依赖
# - git: Git 存储后端需要
# - libpq-dev: PostgreSQL 客户端库
# - gcc: 编译 psycopg2-binary 需要
RUN if [ -n "$APT_MIRROR" ]; then \
        . /etc/os-release; \
        rm -f /etc/apt/sources.list.d/debian.sources; \
        printf 'deb %s %s main contrib non-free non-free-firmware\n' "$APT_MIRROR" "$VERSION_CODENAME" > /etc/apt/sources.list; \
        printf 'deb %s %s-updates main contrib non-free non-free-firmware\n' "$APT_MIRROR" "$VERSION_CODENAME" >> /etc/apt/sources.list; \
        printf 'deb %s-security %s-security main contrib non-free non-free-firmware\n' "$APT_MIRROR" "$VERSION_CODENAME" >> /etc/apt/sources.list; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
    git \
    libpq-dev \
    gcc \
    openssl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY main.py ./
COPY config.json ./
COPY VERSION ./
COPY api ./api
COPY services ./services
COPY utils ./utils
COPY scripts ./scripts
COPY --from=web-build /app/web/out ./web_dist

EXPOSE 80

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80", "--access-log"]

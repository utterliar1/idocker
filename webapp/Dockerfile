# ============================================================
# 爱快 Docker 容器更新器 —— 飞牛 NAS（fnOS）部署用镜像
#
# 设计要点：纯 Python 标准库实现，**不需要 pip install**，
# 因此构建过程不依赖 PyPI，NAS 上构建成功率高、镜像也小。
#
# 构建时唯一需要联网的是基础镜像。若国内拉不到 docker.io，
# 用 --build-arg 换成加速源（compose 里由 .env 的 BASE_IMAGE 传入）：
#   docker compose build --build-arg BASE_IMAGE=docker.1ms.run/library/python:3.13-alpine
# ============================================================
ARG BASE_IMAGE=python:3.13-alpine
FROM ${BASE_IMAGE}

# 版本号：CI 打 tag 时由 git tag 注入（见 .github/workflows/ci.yml），
# 本地直接 docker build 时就用下面的默认值。
ARG APP_VERSION=1.3.3

LABEL org.opencontainers.image.title="idocker" \
      org.opencontainers.image.description="爱快(iKuai) 4.0 Docker 容器更新器，带可视化执行流程" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.source="https://github.com/utterliar1/idocker"

# tzdata 用于让容器内时间正确显示（装了才能让 TZ 生效）。
# Alpine 官方源在国内可能很慢，先尝试国内镜像，全部失败也不影响运行。
RUN set -eux; \
    for m in "" "mirrors.ustc.edu.cn" "mirrors.aliyun.com" "mirrors.tuna.tsinghua.edu.cn"; do \
        if [ -z "$m" ]; then continue; fi; \
        sed -i "s|dl-cdn.alpinelinux.org|$m|g" /etc/apk/repositories || true; \
        if apk add --no-cache tzdata 2>/dev/null; then echo "tzdata <- $m"; exit 0; fi; \
    done; \
    sed -i "s|mirrors\..*alpinelinux.org|dl-cdn.alpinelinux.org|g" /etc/apk/repositories || true; \
    apk add --no-cache tzdata 2>/dev/null || true

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8088 \
    DATA_DIR=/data

WORKDIR /app

# 建一个非 root 用户跑服务，降低风险
RUN adduser -D -u 1000 appuser && mkdir -p /data && chown -R appuser:appuser /data

COPY app/ /app/
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8088
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=6s --start-period=8s --retries=3 \
  CMD ["python", "healthcheck.py"]

CMD ["python", "server.py"]

FROM node:20-alpine

# 安装构建 better-sqlite3 所需的工具
RUN apk add --no-cache python3 make g++ sqlite

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev && npm cache clean --force

COPY server.js ./

# 数据目录
VOLUME ["/data"]

EXPOSE 8080

ENV NODE_ENV=production \
    PORT=8080 \
    DB_PATH=/data/chat.db \
    JWT_SECRET="" \
    MAX_OFFLINE_MSG=500 \
    OFFLINE_MSG_TTL=7

# 非root用户运行
RUN addgroup -S litechat && adduser -S litechat -G litechat && \
    mkdir -p /data && chown litechat:litechat /data

USER litechat

CMD ["node", "server.js"]

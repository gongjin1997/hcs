# ---- HCS 巡检平台前端镜像：构建静态资源 → nginx ----
FROM node:22-alpine AS build
# 内网构建可指定 npm 镜像：docker build --build-arg NPM_REGISTRY=https://mirrors.example.com/npm/
ARG NPM_REGISTRY=https://registry.npmjs.org
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm config set registry ${NPM_REGISTRY} && npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
ENV TZ=Asia/Shanghai
COPY --from=build /web/dist /usr/share/nginx/html
# 默认站点配置；compose 部署时挂载 deploy/nginx/http.conf（或 https.conf）+ snippets 覆盖
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80 443

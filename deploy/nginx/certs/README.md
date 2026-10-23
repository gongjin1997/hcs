HTTPS 证书目录（启用 docker-compose.https.yml 时只读挂载到 web 容器 /etc/nginx/certs）：

- `server.crt`：站点证书（PEM，含中间证书链）
- `server.key`：私钥（PEM，权限 600，不要提交到代码仓库）

内网没有正式证书时可先自签（浏览器会提示不受信任，需把 CA 下发到运维终端）：

    openssl req -x509 -nodes -newkey rsa:2048 -days 825 -keyout server.key -out server.crt \
      -subj "/CN=inspector.example.com" -addext "subjectAltName=DNS:inspector.example.com,IP:10.0.0.10"

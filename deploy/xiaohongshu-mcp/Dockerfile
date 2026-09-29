# xiaohongshu-mcp-pro（https://github.com/GuardSkill/xiaohongshu-mcp-pro）+ 本项目的补丁。
# 固定在审过代码的提交上（只访问小红书自己的域名）；升级前要重新看一遍上游改动，并确认补丁还能打上。
#
# 补丁：
#   0001 发验证码前先勾选「我已阅读并同意」，弹了同意协议确认框就点「同意」再发，
#        否则小红书只弹确认框、短信不会发出。
# 另外装了中文字体，登录弹窗里的页面截图才不会是一排方框。

FROM alpine/git:latest AS src
ARG XHS_MCP_PRO_REF=6183097a86e7f0c5b2efb9913a76cd54a6cfc958
RUN git clone https://github.com/GuardSkill/xiaohongshu-mcp-pro.git /src \
    && git -C /src checkout "$XHS_MCP_PRO_REF"
COPY patches/ /patches/
RUN cd /src && for p in /patches/*.patch; do git apply "$p"; done

FROM golang:1.24 AS builder
WORKDIR /src
COPY --from=src /src .
RUN go mod download && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" -o /out/app .

FROM ubuntu:22.04
ENV TZ=Asia/Shanghai DEBIAN_FRONTEND=noninteractive
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates wget gnupg \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/googlechrome-linux-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/googlechrome-linux-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable fonts-liberation fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /out/app .
RUN mkdir -p /app/images && chmod 777 /app/images
ENV ROD_BROWSER_BIN=/usr/bin/google-chrome
EXPOSE 18060
CMD ["./app"]

FROM golang:1.24-alpine AS builder

WORKDIR /src
COPY go.mod go.sum* ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/sub2apiplus-keeper .

FROM alpine:3.21

RUN apk add --no-cache ca-certificates tzdata
WORKDIR /app
COPY --from=builder /out/sub2apiplus-keeper /usr/local/bin/sub2apiplus-keeper
COPY keeper.example.yaml /app/keeper.yaml
RUN mkdir -p /app/data

ENV KEEPER_CONFIG=/app/keeper.yaml
EXPOSE 38090
ENTRYPOINT ["sub2apiplus-keeper"]

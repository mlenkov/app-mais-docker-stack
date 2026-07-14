# App.mais — Docker Stack

[![Version](https://img.shields.io/badge/version-7.0-blue.svg)](mais_server_setup.py)

Автоматическое развёртывание production-сервера для [Mais Agency](https://mais.agency): **Ghost CMS**, **Bifrost AI Router**, **Yandex OAuth** и **Caddy** с HTTPS, фаерволом и утилитами управления.

## Архитектура

```
                                    ┌────────────────────────────────────────────────────────────────────┐
                                    │                           Caddy                                    │
                                    │                   Container: mais-caddy                            │
                                    │      Networks: mais-caddy-net, mais-ghost-net,                     │
                                    │                mais-bifrost-net                                   │
                                    │      Ports: 80, 443 (публичные)                                   │
                                    └──┬──────────────┬──────────────────┬──────────────────────────────┘
                                       │              │                  │
                              forward_auth            ▼                  │
                                       │      ┌──────────────┐          │
                                       │      │ Yandex Auth  │          │
                                       ▼      │ Container:    │          │
                                 ┌─────────── │ mais-auth     │          │
                                 │  @open     │ Port: 4180    │          │
                                 │  /v1/*     │ Network:      │          │
                                 │  /health   │ mais-caddy-net│          │
                                 │  /mcp/*    └──────────────┘          │
                                 │  (no auth)                           │
                                 └──────┬───────────────────────────────┘
                                        │
                          ┌─────────────┴──────────────┐
                          ▼                             ▼
               ┌──────────────────┐         ┌──────────────────────┐
               │   Ghost CMS       │         │  Bifrost AI Router   │
               │  Container:        │         │  Container:           │
               │  mais-ghost       │         │  mais-bifrost         │
               │  Port: 2368        │         │  Port: 8080           │
               │  Volume:           │         │  Volume:              │
               │  mais-ghost-data   │         │  mais-bifrost-data    │
               │  Network:          │         │  Network:             │
               │  mais-ghost-net    │         │  mais-bifrost-net     │
               └──────────────────┘         └──────────────────────┘
```

Только Caddy публикует порты на хост (80/443). Ghost, Bifrost и Auth доступны только через reverse proxy — стандартная практика безопасности.

Публичные API-эндпоинты (`/v1/*`, `/health`, `/mcp/*`) проходят без аутентификации. Все остальные запросы (`/api/*`, SPA, статика) проверяются через `forward_auth` → Yandex OAuth.

## Требования

- **ОС**: Debian 11+ / Ubuntu 22.04+
- **Права**: `root` (или `sudo`)
- **Домены**: A-записи `mais.agency` и `app.mais.agency`, указывающие на IP сервера
- **Порты**: TCP 80 и 443 доступны извне
- **Yandex OAuth**: приложение на https://oauth.yandex.ru/ с Redirect URI `https://app.mais.agency/oauth2/callback`

## Быстрый старт

```bash
# 1. Установить Jinja2
sudo apt update && sudo apt install -y python3-jinja2

# 2. Запустить установку
sudo python3 mais_server_setup.py
```

Скрипт выполнит все шаги автоматически. Время установки: ~3–5 минут.

## Состав стека

| Сервис | Версия | Образ | Назначение |
|--------|--------|-------|------------|
| **Ghost** | 6.52.0 | `ghost:6.52.0-alpine` | CMS / блог |
| **Bifrost** | v1.6.3 | `maximhq/bifrost:v1.6.3` | AI-роутер / MCP proxy |
| **Caddy** | 2.10.2 | `caddy:2.10.2-alpine` | Reverse proxy + авто-SSL |
| **Yandex Auth** | 1.0.0 | `mais/yandex-auth:latest` | OAuth (собирается из `auth/Dockerfile`) |

## Конфигурация

Все настройки в словаре `CONFIG` в начале скрипта (`mais_server_setup.py:33`):

| Поле | Значение по умолчанию | Описание |
|------|-----------------------|----------|
| `domain_main` | `mais.agency` | Основной домен (Ghost) |
| `domain_www` | `www.mais.agency` | Редирект на основной |
| `domain_app` | `app.mais.agency` | Домен Bifrost |
| `email_acme` | `admin@mais.agency` | Email для Let's Encrypt |
| `ghost_version` | `6.52.0` | Версия Ghost |
| `bifrost_version` | `v1.6.3` | Версия Bifrost |
| `caddy_version` | `2.10.2` | Версия Caddy |

## Что делает скрипт (13 шагов)

1. **Обновление системы** — apt update, установка curl, wget, git, python3-jinja2
2. **ZRAM** — сжатый swap в RAM (lz4, 2x RAM, swappiness=60)
3. **Docker** — установка Docker CE + Compose, настройка registry mirror, log rotation
4. **Фаервол (nftables)** — только SSH (22), HTTP (80), HTTPS (443), ICMP
5. **Секреты** — создание `.env` шаблонов для Bifrost и Auth
6. **Структура папок** — `/opt/docker/{ghost,bifrost,caddy,auth}`
7. **Docker-сети** — три изолированные bridge-сети
8. **Ghost CMS** — деплой, ожидание healthcheck
9. **Bifrost** — деплой, создание пустого `.env` для API-ключей
10. **Yandex Auth** — сборка `mais/yandex-auth`, деплой с `forward_auth`
11. **Caddy** — деплой, генерация Caddyfile с `forward_auth`, проверка DNS
12. **Проверка** — вывод статуса контейнеров, сетей, томов, портов
13. **Утилиты** — установка `mais-status`, `mais-logs`, `mais-restart`

## Пост-установка

После завершения скрипта необходимо вручную:

1. **Security Groups** — открыть TCP 80, 443 от `0.0.0.0/0` в панели облачного провайдера
2. **DNS** — проверить A-записи для `mais.agency` и `app.mais.agency`
3. **API-ключи** — добавить ключи провайдеров в `/opt/secrets/bifrost.env`
4. **OAuth** — заполнить `/opt/secrets/auth.env`:
   ```bash
   YANDEX_CLIENT_ID=ваш_id
   YANDEX_CLIENT_SECRET=ваш_секрет
   COOKIE_SECRET=случайная_строка_32+_символа
   ```
5. **SSL** — Caddy получит сертификаты автоматически. Проверить:
   ```bash
   mais-logs caddy | grep certificate
   ```

## Утилиты управления

| Команда | Описание |
|---------|----------|
| `mais-status` | Статус всех контейнеров |
| `mais-logs ghost\|bifrost\|caddy\|auth` | Логи сервиса |
| `mais-restart ghost\|bifrost\|caddy\|auth\|all` | Перезапуск |

## Структура проекта

```
app-mais-docker-stack/
├── .gitignore
├── LICENSE
├── README.md
├── mais_server_setup.py       # Главный скрипт установки
├── auth/
│   ├── server.py              # Yandex OAuth forward_auth сервер
│   ├── Dockerfile             # Dockerfile для сборки образа
│   └── config.yml             # Конфигурация (rate limit, allowed emails)
├── templates/
│   ├── Caddyfile.j2           # Caddy config с forward_auth
│   ├── caddy-compose.yml.j2   # Caddy + Auth compose
│   ├── auth-compose.yml.j2    # Auth compose (отдельный деплой)
│   ├── bifrost-compose.yml.j2 # Bifrost compose
│   └── ghost-compose.yml.j2   # Ghost compose
└── docs/
    └── steps.md               # Описание шагов установки
```

## OAuth Flow

```
Browser → Caddy → forward_auth → Yandex Auth (:4180)
  ↓ кука _ya_auth валидна              ↓ проверка HMAC-подписи
  ↓                                   ↓
  ↓  ← 200 + X-Auth-Request-User ←   ↓
  ↓                                   ↓
  → Bifrost (:8080) с заголовками пользователя
```

## Лицензия

MIT

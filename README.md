# App.mais — Docker Stack

[![Version](https://img.shields.io/badge/version-6.1-blue.svg)](mais_server_setup.py)

Автоматическое развёртывание production-сервера для [Mais Agency](https://mais.agency): **Ghost CMS**, **Bifrost AI Router** и **Caddy** с HTTPS, фаерволом и утилитами управления.

## Архитектура

```
┌───────────────────────────────────────────────────────────┐
│                         Caddy                              │
│                 Container: mais-caddy                      │
│    Networks: mais-caddy-net, mais-ghost-net,               │
│              mais-bifrost-net                              │
│    Ports: 80, 443 (публичные)                              │
└───────┬───────────────────────────┬───────────────────────┘
        │                           │
        ▼                           ▼
┌──────────────────┐     ┌──────────────────────┐
│   Ghost CMS       │     │  Bifrost AI Router   │
│  Container:        │     │  Container:           │
│  mais-ghost       │     │  mais-bifrost         │
│  Port: 2368        │     │  Port: 8080           │
│  Volume:           │     │  Volume:              │
│  mais-ghost-data   │     │  mais-bifrost-data    │
│  Network:          │     │  Network:             │
│  mais-ghost-net    │     │  mais-bifrost-net     │
└──────────────────┘     └──────────────────────┘
```

Только Caddy публикует порты на хост (80/443). Ghost и Bifrost доступны только через reverse proxy — стандартная практика безопасности.

## Требования

- **ОС**: Debian 11+ / Ubuntu 22.04+
- **Права**: `root` (или `sudo`)
- **Домены**: A-записи `mais.agency` и `app.mais.agency`, указывающие на IP сервера
- **Порты**: TCP 80 и 443 доступны извне

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
| **Bifrost** | 1.4.9 | `maximhq/bifrost:1.4.9` | AI-роутер / MCP proxy |
| **Caddy** | 2.10.2 | `caddy:2.10.2-alpine` | Reverse proxy + авто-SSL |

## Конфигурация

Все настройки в словаре `CONFIG` в начале скрипта (`mais_server_setup.py:26`):

| Поле | Значение по умолчанию | Описание |
|------|-----------------------|----------|
| `domain_main` | `mais.agency` | Основной домен (Ghost) |
| `domain_www` | `www.mais.agency` | Редирект на основной |
| `domain_app` | `app.mais.agency` | Домен Bifrost |
| `email_acme` | `admin@mais.agency` | Email для Let's Encrypt |
| `ghost_version` | `6.52.0` | Версия Ghost |
| `bifrost_version` | `1.4.9` | Версия Bifrost |
| `caddy_version` | `2.10.2` | Версия Caddy |

## Что делает скрипт (10 шагов)

1. **Обновление системы** — apt update, установка curl, wget, git, nftables, python3-jinja2
2. **Docker** — установка Docker CE + Compose, настройка registry mirror, log rotation
3. **Фаервол (nftables)** — только SSH (22), HTTP (80), HTTPS (443), ICMP
4. **Структура папок** — `/opt/docker/{ghost,bifrost,caddy}`
5. **Docker-сети** — три изолированные bridge-сети
6. **Ghost CMS** — деплой, ожидание healthcheck
7. **Bifrost** — деплой, создание пустого `.env` для API-ключей
8. **Caddy** — деплой, генрация Caddyfile, проверка DNS
9. **Проверка** — вывод статуса контейнеров, сетей, томов, портов
10. **Утилиты** — установка `mais-status`, `mais-logs`, `mais-restart`

## Пост-установка

После завершения скрипта необходимо вручную:

1. **Security Groups** — открыть TCP 80, 443 от `0.0.0.0/0` в панели облачного провайдера
2. **DNS** — проверить A-записи для `mais.agency` и `app.mais.agency`
3. **API-ключи Bifrost** — отредактировать `/opt/docker/bifrost/.env`:
   ```
   OPENAI_API_KEY=sk-...
   ANTHROPIC_API_KEY=sk-...
   ```
4. **SSL** — Caddy получит сертификаты автоматически. Проверить:
   ```bash
   mais-logs caddy | grep certificate
   ```

## Утилиты управления

| Команда | Описание |
|---------|----------|
| `mais-status` | Статус всех контейнеров |
| `mais-logs ghost`, `mais-logs bifrost`, `mais-logs caddy` | Логи сервиса |
| `mais-restart ghost`, `mais-restart all` | Перезапуск |

## Структура проекта

```
app-docker-setup/
├── .gitignore
├── LICENSE
├── README.md
├── mais_server_setup.py   # Главный скрипт установки (688 строк)
├── .gigacode/
│   └── plans/             # Планы развития
└── docs/
    └── steps.md           # Описание шагов установки
```

## Лицензия

MIT

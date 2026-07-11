# Пошаговое описание установки

Скрипт `mais_server_setup.py` выполняет 10 последовательных шагов.

## Шаг 1. Обновление системы (`update_system`)

- `apt-get update`
- Установка: curl, wget, git, ca-certificates, gnupg, lsb-release, nftables, python3-jinja2

## Шаг 2. Установка Docker (`install_docker`)

- Удаление старых версий Docker
- Добавление официального репозитория Docker для Debian
- Установка: docker-ce, docker-ce-cli, containerd.io, docker-buildx-plugin, docker-compose-plugin
- Настройка `daemon.json`: registry mirror (`dh-mirror.gitverse.ru`), log rotation (10MB, 3 файла)
- Добавление пользователя `mais` в группу `docker`
- Включение и запуск Docker systemd-сервиса

## Шаг 3. Настройка фаервола (`configure_firewall`)

- Загрузка модуля `br_netfilter`
- Настройка sysctl: bridge-nf-call-iptables, ip_forward, rp_filter
- nftables ruleset:
  - Default policy: DROP на вход
  - Разрешено: established/related, loopback, ICMP, SSH (22), HTTP (80), HTTPS (443)
  - Forward: DROP (но ACCEPT для трафика Docker)
  - Output: ACCEPT

## Шаг 4. Создание структуры папок (`setup_directories`)

- `/opt/docker/ghost/`
- `/opt/docker/bifrost/`
- `/opt/docker/caddy/`
- Владелец: `mais:mais`

## Шаг 5. Создание Docker-сетей (`setup_networks`)

Три bridge-сети (создаются, если не существуют):

| Сеть | Назначение |
|------|------------|
| `mais-caddy-net` | Публичные сервисы (Caddy) |
| `mais-ghost-net` | Ghost CMS |
| `mais-bifrost-net` | Bifrost + MCP |

## Шаг 6. Ghost CMS (`setup_ghost`)

- Генерация `compose.yml` из Jinja2-шаблона
- Запуск: `docker compose up -d`
- Ожидание healthcheck (порт 2368, max 90 секунд)

## Шаг 7. Bifrost (`setup_bifrost`)

- Создание пустого `.env` с комментариями-заглушками для API-ключей
- Генерация `compose.yml` из Jinja2-шаблона
- Запуск: `docker compose up -d`
- Ожидание healthcheck (порт 8080, max 60 секунд)

## Шаг 8. Caddy (`setup_caddy`)

- Проверка DNS для `mais.agency` и `app.mais.agency` (предупреждение, если не резолвятся)
- Генерация `compose.yml` и `Caddyfile` из Jinja2-шаблона
- Запуск: `docker compose up -d`
- Ожидание healthcheck (порт 80, max 30 секунд)

## Шаг 9. Проверка (`verify`)

- Вывод таблицы контейнеров (фильтр: `label=project=mais`)
- Вывод сетей (фильтр: `name=mais-`)
- Вывод томов (фильтр: `name=mais-`)
- Проверка, что порты 80 и 443 слушаются на хосте

## Шаг 10. Утилиты (`create_utils`)

Три скрипта в `/usr/local/bin/`:

| Скрипт | Описание |
|--------|----------|
| `mais-status` | Показывает статус всех контейнеров стека |
| `mais-logs` | Хвостит логи указанного сервиса (ghost/bifrost/caddy) |
| `mais-restart` | Перезапускает указанный сервис (ghost/bifrost/caddy/all) |

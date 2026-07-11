# Пошаговое описание установки

Скрипт `mais_server_setup.py` (v7.0) выполняет 13 последовательных шагов.

## Шаг 0. Обновление системы (`update_system`)

- `apt-get update`
- Установка: curl, wget, git, ca-certificates, gnupg, python3-jinja2
- **nftables не устанавливается** — Base-слой уже настроил фаервол

## Шаг 1. ZRAM (`setup_zram`)

- Вместо статичного swap-файла на SSD
- Пытается установить `systemd-zram-generator`; если недоступен — `zram-tools`
- Конфиг: `zram-size = ram * 2`, `lz4` (быстрое сжатие), `swap-priority = 100`
- `vm.swappiness = 60` через `/etc/sysctl.d/99-mais-zram.conf`

## Шаг 2. Установка Docker (`install_docker`)

- Удаление старых версий Docker
- Добавление официального репозитория Docker для Debian
- Установка: docker-ce, docker-ce-cli, containerd.io, docker-buildx-plugin, docker-compose-plugin
- Добавление пользователя `mais` в группу `docker`

### Настройка Docker Daemon (`configure_docker_daemon`)

- Чтение существующего `/etc/docker/daemon.json` (слияние конфигов)
- Registry mirror: `dh-mirror.gitverse.ru`
- Log rotation: `max-size=10m`, `max-file=3`

## Шаг 3. Фаервол (nftables drop-in) (`setup_firewall_rules`)

- **Не перезаписывает** `/etc/nftables.conf` Base-слоя
- Создаёт `/etc/nftables.d/99-mais-app.nft` с правилами для портов 80/443
- Добавляет `include "/etc/nftables.d/*.nft"` в главный конфиг (если отсутствует)
- Применяет правила идемпотентно (проверяет наличие `app-mais-http-https`)

### Sysctl (`configure_sysctl`)

- `/etc/sysctl.d/99-mais-docker.conf` — bridge-nf-call-iptables, ip_forward, rp_filter
- Загрузка модуля `br_netfilter`

## Шаг 4. Секреты (`setup_secrets`, `setup_app_user`)

- Создаёт `/opt/secrets/bifrost.env` (chmod 600), если не существует
- Создаёт системного пользователя `appuser` (UID 1000) для запуска контейнеров не от root

## Шаг 5. Структура папок (`setup_directories`)

- `/opt/docker/ghost/`
- `/opt/docker/bifrost/`
- `/opt/docker/caddy/`
- Владелец: `mais:mais`

## Шаг 6. Docker-сети (`setup_networks`)

Три bridge-сети (создаются, если не существуют):

| Сеть | Назначение |
|------|------------|
| `mais-caddy-net` | Публичные сервисы (Caddy) |
| `mais-ghost-net` | Ghost CMS |
| `mais-bifrost-net` | Bifrost + MCP |

## Шаг 7. Ghost CMS (`setup_ghost`)

- Генерация `compose.yml` из `templates/ghost-compose.yml.j2`
- CPU лимит: 0.5, RAM лимит: 768M, Node memory: 512MB
- `user: "1000:1000"` (не root)
- Порт: 2368 (internal)
- Запуск и ожидание healthcheck (max 90 секунд)

## Шаг 8. Bifrost (`setup_bifrost`)

- Генерация `compose.yml` из `templates/bifrost-compose.yml.j2`
- `env_file: /opt/secrets/bifrost.env` (безопасное хранение ключей)
- CPU лимит: 0.5, RAM лимит: 512M
- `user: "1000:1000"` (не root)
- Порт: 8080 (internal)
- Запуск и ожидание healthcheck (max 60 секунд)

## Шаг 9. Caddy (`setup_caddy`)

- Проверка DNS для `mais.agency` и `app.mais.agency`
- Генерация `compose.yml` и `Caddyfile` из шаблонов
- CPU лимит: 0.3, RAM лимит: 128M
- Rate limiting в Caddyfile (20 rps)
- Порты: 80, 443 (публичные)
- Запуск и ожидание healthcheck (max 30 секунд)

## Шаг 10. Проверка (`verify`)

- Вывод таблицы контейнеров (фильтр: `project=mais`)
- Вывод сетей и томов
- Проверка портов 80/443 на хосте
- Проверка активности ZRAM

## Шаг 11. Обслуживание диска (`setup_maintenance`)

- Cron-задача `/etc/cron.d/mais-docker-prune`
- `docker system prune -af --volumes` каждое воскресенье в 3:00
- Логи через `logger`

## Шаг 12. Утилиты (`create_utils`)

| Скрипт | Описание |
|--------|----------|
| `mais-status` | Статус всех контейнеров стека |
| `mais-logs` | Логи сервиса (ghost/bifrost/caddy) |
| `mais-restart` | Перезапуск (ghost/bifrost/caddy/all) |

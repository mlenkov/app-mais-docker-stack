#!/usr/bin/env python3
"""
Скрипт автоматической установки и настройки сервера для Mais Agency
Версия: 6.1 (Единый стиль именования, Jinja2 safe, project_name)
"""

import subprocess
import sys
import os
import time
import json
import socket
from pathlib import Path

try:
    from jinja2 import Template
except ImportError:
    print("Ошибка: требуется jinja2. Установите: pip3 install jinja2")
    sys.exit(1)

SCRIPT_VERSION = "6.1"

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
CONFIG = {
    # Домены
    "domain_main": "mais.agency",
    "domain_www": "www.mais.agency",
    "domain_app": "app.mais.agency",
    "email_acme": "admin@mais.agency",

    # Пути
    "docker_dir": "/opt/docker",
    "user": "mais",

    # Версии образов
    "ghost_version": "6.52.0",
    "bifrost_version": "1.4.9",
    "caddy_version": "2.10.2",

    # Имена контейнеров (kebab-case)
    "container_ghost": "mais-ghost",
    "container_bifrost": "mais-bifrost",
    "container_caddy": "mais-caddy",

    # Сети (kebab-case)
    "net_caddy": "mais-caddy-net",
    "net_ghost": "mais-ghost-net",
    "net_bifrost": "mais-bifrost-net",

    # Volumes (kebab-case)
    "vol_ghost_data": "mais-ghost-data",
    "vol_bifrost_data": "mais-bifrost-data",
    "vol_caddy_data": "mais-caddy-data",
    "vol_caddy_config": "mais-caddy-config",
}

# ============================================================
# ШАБЛОНЫ КОНФИГОВ
# ============================================================
GHOST_COMPOSE_TPL = """name: mais
services:
  ghost:
    image: ghost:{{ ghost_version }}-alpine
    container_name: {{ container_ghost }}
    restart: unless-stopped
    expose:
      - "2368"
    environment:
      NODE_ENV: production
      url: https://{{ domain_main }}
      database__client: sqlite3
      database__connection__filename: /var/lib/ghost/content/data/ghost.db
    volumes:
      - {{ vol_ghost_data }}:/var/lib/ghost/content
    networks:
      - {{ net_ghost }}
    labels:
      project: "mais"
      service: "ghost"
      environment: "production"
    healthcheck:
      test: ["CMD", "wget", "--quiet", "--tries=1", "--spider", "http://localhost:2368/"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s

volumes:
  {{ vol_ghost_data }}:
    name: {{ vol_ghost_data }}

networks:
  {{ net_ghost }}:
    external: true
    name: {{ net_ghost }}
"""

BIFROST_COMPOSE_TPL = """name: mais
services:
  bifrost:
    image: maximhq/bifrost:{{ bifrost_version }}
    container_name: {{ container_bifrost }}
    restart: unless-stopped
    expose:
      - "8080"
    env_file: .env
    environment:
      APP_PORT: 8080
      APP_HOST: 0.0.0.0
      LOG_LEVEL: info
      LOG_STYLE: json
    volumes:
      - {{ vol_bifrost_data }}:/app/data
    networks:
      - {{ net_bifrost }}
    labels:
      project: "mais"
      service: "bifrost"
      environment: "production"
    healthcheck:
      test: ["CMD", "wget", "--quiet", "--tries=1", "--spider", "http://localhost:8080/"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 20s

volumes:
  {{ vol_bifrost_data }}:
    name: {{ vol_bifrost_data }}

networks:
  {{ net_bifrost }}:
    external: true
    name: {{ net_bifrost }}
"""

CADDY_COMPOSE_TPL = """name: mais
services:
  caddy:
    image: caddy:{{ caddy_version }}-alpine
    container_name: {{ container_caddy }}
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - {{ vol_caddy_data }}:/data
      - {{ vol_caddy_config }}:/config
    networks:
      - {{ net_caddy }}
      - {{ net_ghost }}
      - {{ net_bifrost }}
    labels:
      project: "mais"
      service: "caddy"
      environment: "production"
    healthcheck:
      test: ["CMD", "wget", "--quiet", "--tries=1", "--spider", "http://localhost:80/"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

volumes:
  {{ vol_caddy_data }}:
    name: {{ vol_caddy_data }}
  {{ vol_caddy_config }}:
    name: {{ vol_caddy_config }}

networks:
  {{ net_caddy }}:
    external: true
    name: {{ net_caddy }}
  {{ net_ghost }}:
    external: true
    name: {{ net_ghost }}
  {{ net_bifrost }}:
    external: true
    name: {{ net_bifrost }}
"""

# {% raw %} защищает фигурные скобки Caddy от интерпретации Jinja2
CADDYFILE_TPL = """{% raw %}{
    email {% endraw %}{{ email_acme }}{% raw %}
    log {
        output stdout
        format json
    }
}

# Ghost CMS
{% endraw %}{{ domain_main }}{% raw %} {
    reverse_proxy {% endraw %}{{ container_ghost }}{% raw %}:2368 {
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
    encode gzip
    @static {
        path *.css *.js *.png *.jpg *.jpeg *.gif *.ico *.woff *.woff2 *.ttf
    }
    handle @static {
        header Cache-Control "public, max-age=31536000"
    }
}

# Редирект www → основной домен
{% endraw %}{{ domain_www }}{% raw %} {
    redir https://{% endraw %}{{ domain_main }}{% raw %}{uri} permanent
}

# Bifrost AI Router
{% endraw %}{{ domain_app }}{% raw %} {
    reverse_proxy {% endraw %}{{ container_bifrost }}{% raw %}:8080
    encode gzip
}
{% endraw %}"""

# ============================================================
# УТИЛИТЫ
# ============================================================
class Colors:
    HEADER = '\033[95m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'


def print_header(text):
    print(f"\n{Colors.HEADER}{Colors.BOLD}{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}{Colors.ENDC}\n")


def print_step(text):
    print(f"\n{Colors.OKCYAN}▶ {text}{Colors.ENDC}")


def print_success(text):
    print(f"{Colors.OKGREEN}✓ {text}{Colors.ENDC}")


def print_warning(text):
    print(f"{Colors.WARNING}⚠ {text}{Colors.ENDC}")


def print_error(text):
    print(f"{Colors.FAIL}✗ {text}{Colors.ENDC}")


def run_command(cmd, check=True, capture=False):
    """Безопасный запуск команд без shell=True"""
    try:
        if isinstance(cmd, str):
            cmd = ["bash", "-c", cmd]

        if capture:
            result = subprocess.run(cmd, check=check, capture_output=True, text=True)
            return result.stdout.strip()
        else:
            result = subprocess.run(cmd, check=check)
            return result.returncode == 0
    except subprocess.CalledProcessError as e:
        if check:
            print_error(f"Ошибка выполнения: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
            if e.stderr:
                print_error(f"stderr: {e.stderr.strip()}")
        return False


def wait_for_input(message="Нажмите Enter для продолжения..."):
    input(f"\n{Colors.WARNING}{message}{Colors.ENDC}")


def check_root():
    if os.geteuid() != 0:
        print_error("Запустите с правами root: sudo python3 setup_server.py")
        sys.exit(1)


def render_template(template_str, context):
    """Безопасная генерация конфигов через Jinja2"""
    tpl = Template(template_str)
    return tpl.render(**context)


def write_config(path, template_str, context):
    """Рендерит шаблон и записывает файл"""
    content = render_template(template_str, context)
    with open(path, 'w') as f:
        f.write(content)
    print_success(f"Создан конфиг: {path}")


def wait_for_container(name, timeout=60, interval=3):
    """Ждет пока контейнер станет healthy или running"""
    print_step(f"Ожидание запуска {name} (макс. {timeout}с)...")
    elapsed = 0
    while elapsed < timeout:
        status = run_command(
            ["docker", "inspect", "--format={{.State.Health.Status}}", name],
            check=False, capture=True
        )
        if not status or status == "<no value>":
            status = run_command(
                ["docker", "inspect", "--format={{.State.Running}}", name],
                check=False, capture=True
            )
            if status == "true":
                print_success(f"{name} запущен (без healthcheck)")
                return True

        if status in ("healthy", "running"):
            print_success(f"{name} готов ({elapsed}с)")
            return True

        time.sleep(interval)
        elapsed += interval

    print_warning(f"Таймаут ожидания {name} ({timeout}с). Проверьте: docker logs {name}")
    return False


def check_dns(domain):
    """Проверяет резолвинг домена"""
    try:
        socket.gethostbyname(domain)
        return True
    except socket.gaierror:
        return False


def network_exists(name):
    """Проверяет существование Docker-сети"""
    result = run_command(
        ["docker", "network", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"],
        check=False, capture=True
    )
    return result == name


# ============================================================
# ШАГ 1: ОБНОВЛЕНИЕ СИСТЕМЫ
# ============================================================
def update_system():
    print_header("1. Обновление системы")
    run_command(["apt-get", "update", "-qq"])
    run_command([
        "apt-get", "install", "-y", "-qq",
        "curl", "wget", "git", "ca-certificates",
        "gnupg", "lsb-release", "nftables", "python3-jinja2"
    ])
    print_success("Система обновлена")


# ============================================================
# ШАГ 2: УСТАНОВКА DOCKER
# ============================================================
def install_docker():
    print_header("2. Установка Docker")

    if run_command(["docker", "--version"], check=False, capture=True):
        print_warning("Docker уже установлен")
    else:
        run_command(["apt-get", "remove", "-y",
                      "docker", "docker-engine", "docker.io", "containerd", "runc"],
                     check=False)
        run_command(["install", "-m", "0755", "-d", "/etc/apt/keyrings"])
        run_command([
            "curl", "-fsSL",
            "https://download.docker.com/linux/debian/gpg",
            "-o", "/etc/apt/keyrings/docker.asc"
        ])
        run_command(["chmod", "a+r", "/etc/apt/keyrings/docker.asc"])

        codename = run_command(
            ["bash", "-c", ". /etc/os-release && echo $VERSION_CODENAME"],
            capture=True
        )
        repo_line = (
            f"deb [arch=$(dpkg --print-architecture) "
            f"signed-by=/etc/apt/keyrings/docker.asc] "
            f"https://download.docker.com/linux/debian {codename} stable"
        )
        run_command(["bash", "-c", f"echo '{repo_line}' > /etc/apt/sources.list.d/docker.list"])
        run_command(["apt-get", "update", "-qq"])
        run_command([
            "apt-get", "install", "-y", "-qq",
            "docker-ce", "docker-ce-cli", "containerd.io",
            "docker-buildx-plugin", "docker-compose-plugin"
        ])
        print_success("Docker установлен")

    daemon_config = {
        "registry-mirrors": ["https://dh-mirror.gitverse.ru"],
        "log-driver": "json-file",
        "log-opts": {"max-size": "10m", "max-file": "3"}
    }
    with open("/etc/docker/daemon.json", 'w') as f:
        json.dump(daemon_config, f, indent=2)
    run_command(["systemctl", "restart", "docker"])

    run_command(["usermod", "-aG", "docker", CONFIG['user']], check=False)
    run_command(["systemctl", "enable", "docker"])
    run_command(["systemctl", "start", "docker"])
    print_success("Docker настроен")


# ============================================================
# ШАГ 3: ФАЕРВОЛ
# ============================================================
def configure_firewall():
    print_header("3. Настройка фаервола")

    run_command(["modprobe", "br_netfilter"])
    modules_path = "/etc/modules-load.d/br_netfilter.conf"
    if not os.path.exists(modules_path):
        with open(modules_path, "w") as f:
            f.write("br_netfilter\n")

    sysctl_conf = """net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
net.ipv4.conf.all.rp_filter = 0
"""
    with open("/etc/sysctl.d/99-docker.conf", "w") as f:
        f.write(sysctl_conf)
    run_command(["sysctl", "--system"])

    nftables_conf = """#!/usr/sbin/nft -f
flush ruleset

table inet filter {
    chain input {
        type filter hook input priority filter; policy drop;
        ct state established,related accept
        iifname "lo" accept
        icmp type { echo-reply, echo-request } accept
        tcp dport 22 accept
        tcp dport { 80, 443 } accept
        udp dport 443 accept
    }
    chain forward {
        type filter hook forward priority filter; policy drop;
        accept
    }
    chain output {
        type filter hook output priority filter; policy accept;
    }
}
"""
    with open("/etc/nftables.conf", "w") as f:
        f.write(nftables_conf)
    run_command(["nft", "-f", "/etc/nftables.conf"], check=False)
    run_command(["systemctl", "enable", "nftables"], check=False)
    print_success("Фаервол настроен")


# ============================================================
# ШАГ 4: СТРУКТУРА ПАПОК
# ============================================================
def setup_directories():
    print_header("4. Создание структуры папок")

    dirs = [
        f"{CONFIG['docker_dir']}/ghost",
        f"{CONFIG['docker_dir']}/bifrost",
        f"{CONFIG['docker_dir']}/caddy",
    ]

    for dir_path in dirs:
        Path(dir_path).mkdir(parents=True, exist_ok=True)

    run_command(["chown", "-R", f"{CONFIG['user']}:{CONFIG['user']}", CONFIG['docker_dir']])
    print_success("Структура создана")


# ============================================================
# ШАГ 5: СЕТИ (ИДЕМПОТЕНТНО)
# ============================================================
def setup_networks():
    print_header("5. Создание сетей")

    networks = [
        (CONFIG['net_caddy'], "Публичные сервисы (Caddy)"),
        (CONFIG['net_ghost'], "Ghost CMS"),
        (CONFIG['net_bifrost'], "Bifrost + MCP"),
    ]

    for net_name, description in networks:
        if network_exists(net_name):
            print_warning(f"Сеть {net_name} уже существует — пропускаем")
        else:
            run_command(["docker", "network", "create", "--driver", "bridge", net_name])
            print_success(f"Сеть {net_name} создана ({description})")


# ============================================================
# ШАГ 6: GHOST
# ============================================================
def setup_ghost():
    print_header("6. Настройка Ghost")

    ghost_dir = f"{CONFIG['docker_dir']}/ghost"
    write_config(f"{ghost_dir}/compose.yml", GHOST_COMPOSE_TPL, CONFIG)

    os.chdir(ghost_dir)
    run_command(["docker", "compose", "up", "-d"])
    wait_for_container(CONFIG['container_ghost'], timeout=90)


# ============================================================
# ШАГ 7: BIFROST
# ============================================================
def setup_bifrost():
    print_header("7. Настройка Bifrost")

    bifrost_dir = f"{CONFIG['docker_dir']}/bifrost"

    env_path = f"{bifrost_dir}/.env"
    if not os.path.exists(env_path):
        with open(env_path, 'w') as f:
            f.write("# Bifrost API Keys\n# OPENAI_API_KEY=\n# ANTHROPIC_API_KEY=\n")
        print_warning("Создан пустой .env для Bifrost — добавьте API-ключи!")

    write_config(f"{bifrost_dir}/compose.yml", BIFROST_COMPOSE_TPL, CONFIG)

    os.chdir(bifrost_dir)
    run_command(["docker", "compose", "up", "-d"])
    wait_for_container(CONFIG['container_bifrost'], timeout=60)


# ============================================================
# ШАГ 8: CADDY
# ============================================================
def setup_caddy():
    print_header("8. Настройка Caddy")

    for domain in [CONFIG['domain_main'], CONFIG['domain_app']]:
        if not check_dns(domain):
            print_warning(f"DNS не настроен для {domain} — Caddy не получит SSL!")
            print_warning("Настройте A-запись и перезапустите: cd /opt/docker/caddy && docker compose restart")

    caddy_dir = f"{CONFIG['docker_dir']}/caddy"
    write_config(f"{caddy_dir}/compose.yml", CADDY_COMPOSE_TPL, CONFIG)
    write_config(f"{caddy_dir}/Caddyfile", CADDYFILE_TPL, CONFIG)

    os.chdir(caddy_dir)
    run_command(["docker", "compose", "up", "-d"])
    wait_for_container(CONFIG['container_caddy'], timeout=30)


# ============================================================
# ШАГ 9: ПРОВЕРКА
# ============================================================
def verify():
    print_header("9. Проверка")

    print("\nКонтейнеры:")
    print(run_command(
        ["docker", "ps", "--format", "table {{.Names}}\t{{.Status}}",
         "--filter", "label=project=mais"],
        capture=True
    ))

    print("\nСети:")
    print(run_command(
        ["docker", "network", "ls", "--filter", "name=mais-"],
        capture=True
    ))

    print("\nVolumes:")
    print(run_command(
        ["docker", "volume", "ls", "--filter", "name=mais-"],
        capture=True
    ))

    print("\nПорты:")
    result = run_command(["bash", "-c", "ss -tlnp | grep -E ':80|:443'"], capture=True)
    if result:
        print(result)
        print_success("Порты 80/443 активны")
    else:
        print_warning("Порты 80/443 не обнаружены!")


# ============================================================
# ШАГ 10: УТИЛИТЫ
# ============================================================
def create_utils():
    print_header("10. Утилиты")

    scripts = {
        "status": '#!/bin/bash\necho "=== Контейнеры ==="\ndocker ps --format "table {{.Names}}\\t{{.Status}}" --filter "label=project=mais"\necho -e "\\n=== Сети ==="\ndocker network ls --filter "name=mais-"\necho -e "\\n=== Volumes ==="\ndocker volume ls --filter "name=mais-"\n',
        "logs": '#!/bin/bash\nSERVICE=$1\ncase $SERVICE in\n    ghost) docker logs -f --tail 50 mais-ghost ;;\n    bifrost) docker logs -f --tail 50 mais-bifrost ;;\n    caddy) docker logs -f --tail 50 mais-caddy ;;\n    *) echo "Использование: mais-logs [ghost|bifrost|caddy]" ;;\nesac\n',
        "restart": '#!/bin/bash\nSERVICE=$1\ncase $SERVICE in\n    ghost) docker restart mais-ghost ;;\n    bifrost) docker restart mais-bifrost ;;\n    caddy) docker restart mais-caddy ;;\n    all) docker restart mais-ghost mais-bifrost mais-caddy ;;\n    *) echo "Использование: mais-restart [ghost|bifrost|caddy|all]" ;;\nesac\n',
    }

    for name, content in scripts.items():
        path = f"/usr/local/bin/mais-{name}"
        with open(path, 'w') as f:
            f.write(content)
        run_command(["chmod", "+x", path])

    print_success("Созданы: mais-status, mais-logs, mais-restart")


# ============================================================
# ФИНАЛ
# ============================================================
def final():
    print_header("✓ УСТАНОВКА ЗАВЕРШЕНА")

    print(f"""
{Colors.OKGREEN}Установлено:{Colors.ENDC}
  • Ghost {CONFIG['ghost_version']} → {CONFIG['domain_main']}
  • Bifrost {CONFIG['bifrost_version']} → {CONFIG['domain_app']}
  • Caddy {CONFIG['caddy_version']} (reverse proxy + HTTPS)

{Colors.OKGREEN}Имена:{Colors.ENDC}
  • Контейнеры: mais-ghost, mais-bifrost, mais-caddy
  • Сети: mais-caddy-net, mais-ghost-net, mais-bifrost-net
  • Volumes: mais-ghost-data, mais-bifrost-data, mais-caddy-data

{Colors.OKGREEN}Команды:{Colors.ENDC}
  • mais-status   - статус сервисов
  • mais-logs     - логи (mais-logs ghost|bifrost|caddy)
  • mais-restart  - перезапуск

{Colors.WARNING}Следующие шаги:{Colors.ENDC}
  1. Security Groups: открыть TCP 80, 443 от 0.0.0.0/0
  2. DNS: mais.agency и app.mais.agency → IP сервера
  3. Добавить API-ключи в /opt/docker/bifrost/.env
  4. Подождать получения SSL-сертификатов Caddy

{Colors.OKCYAN}Проверка SSL:{Colors.ENDC}
  mais-logs caddy | grep certificate
""")


# ============================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================
def main():
    print_header(f"УСТАНОВКА MAIS AGENCY v{SCRIPT_VERSION}")

    print(f"""
{Colors.OKCYAN}Конфигурация:{Colors.ENDC}
  • Домены: {CONFIG['domain_main']}, {CONFIG['domain_app']}
  • Ghost: {CONFIG['ghost_version']}
  • Bifrost: {CONFIG['bifrost_version']}
  • Caddy: {CONFIG['caddy_version']}
""")

    wait_for_input("Начать установку?")

    try:
        update_system()
        install_docker()
        configure_firewall()
        setup_directories()
        setup_networks()
        setup_ghost()
        setup_bifrost()
        setup_caddy()
        verify()
        create_utils()
        final()
    except KeyboardInterrupt:
        print_error("\nПрервано пользователем")
        sys.exit(1)
    except Exception as e:
        print_error(f"Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    check_root()
    main()
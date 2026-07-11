#!/usr/bin/env python3
"""
Скрипт автоматической установки и настройки сервера для Mais Agency
Версия: 7.0 (Refactored: ZRAM, resource limits, idempotent, nftables drop-in)
"""

import subprocess
import sys
import os
import time
import json
import socket
import pwd
import stat
import re
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    print("Ошибка: требуется jinja2. Установите: pip3 install jinja2")
    sys.exit(1)

SCRIPT_VERSION = "7.0"
SCRIPT_DIR = Path(__file__).parent.resolve()
TEMPLATES_DIR = SCRIPT_DIR / "templates"

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
CONFIG = {
    "domain_main": "mais.agency",
    "domain_www": "www.mais.agency",
    "domain_app": "app.mais.agency",
    "email_acme": "admin@mais.agency",

    "docker_dir": "/opt/docker",
    "user": "mais",

    "ghost_version": "6.52.0",
    "bifrost_version": "1.4.9",
    "caddy_version": "2.10.2",

    "container_ghost": "mais-ghost",
    "container_bifrost": "mais-bifrost",
    "container_caddy": "mais-caddy",

    "net_caddy": "mais-caddy-net",
    "net_ghost": "mais-ghost-net",
    "net_bifrost": "mais-bifrost-net",

    "vol_ghost_data": "mais-ghost-data",
    "vol_bifrost_data": "mais-bifrost-data",
    "vol_caddy_data": "mais-caddy-data",
    "vol_caddy_config": "mais-caddy-config",

    "ghost_cpus": "0.5",
    "ghost_memory": "768M",
    "ghost_node_memory": "512",
    "bifrost_cpus": "0.5",
    "bifrost_memory": "512M",
    "caddy_cpus": "0.3",
    "caddy_memory": "128M",

    "app_uid": 1000,
    "app_gid": 1000,

    "bifrost_env_path": "/opt/secrets/bifrost.env",
}

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


def run_command(cmd, check=True, capture=False, timeout=120):
    try:
        if isinstance(cmd, str):
            cmd = ["bash", "-c", cmd]
        if capture:
            result = subprocess.run(cmd, check=check, capture_output=True, text=True, timeout=timeout)
            return result.stdout.strip()
        else:
            result = subprocess.run(cmd, check=check, timeout=timeout)
            return result.returncode == 0
    except subprocess.CalledProcessError as e:
        if check:
            cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd
            print_error(f"Ошибка выполнения: {cmd_str}")
            if e.stderr:
                print_error(f"stderr: {e.stderr.strip()}")
        return False
    except subprocess.TimeoutExpired:
        cmd_str = ' '.join(cmd) if isinstance(cmd, list) else cmd
        print_warning(f"Таймаут команды: {cmd_str}")
        return False


def wait_for_input(message="Нажмите Enter для продолжения..."):
    input(f"\n{Colors.WARNING}{message}{Colors.ENDC}")


def check_root():
    if os.geteuid() != 0:
        print_error("Запустите с правами root: sudo python3 setup_server.py")
        sys.exit(1)


def wait_for_container(name, timeout=60, interval=3):
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
    try:
        socket.gethostbyname(domain)
        return True
    except socket.gaierror:
        return False


def network_exists(name):
    result = run_command(
        ["docker", "network", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"],
        check=False, capture=True
    )
    return result == name


def container_exists(name):
    result = run_command(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        check=False, capture=True
    )
    return result == name


def volume_exists(name):
    result = run_command(
        ["docker", "volume", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"],
        check=False, capture=True
    )
    return result == name


# ============================================================
# РАБОТА С ШАБЛОНАМИ
# ============================================================
def render_template(template_name, context):
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    tpl = env.get_template(template_name)
    return tpl.render(**context)


def write_config(path, template_name, context):
    content = render_template(template_name, context)
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    path_obj.write_text(content)
    print_success(f"Создан конфиг: {path}")


def file_contains(path, pattern):
    if not os.path.exists(path):
        return False
    with open(path) as f:
        return re.search(pattern, f.read()) is not None


# ============================================================
# ШАГ 1: ZRAM (вместо статичного swap)
# ============================================================
def setup_zram():
    print_header("1. Настройка ZRAM")

    existing_swap = run_command(["swapon", "--show", "--noheadings"], capture=True, check=False)
    if "zram" in existing_swap:
        print_success("ZRAM уже активен")
        return

    run_command(["apt-get", "install", "-y", "-qq", "systemd-zram-generator"], check=False)

    gen_path = Path("/lib/systemd/system-generators/systemd-zram-generator")
    if gen_path.exists():
        zram_conf = """[zram0]
zram-size = ram * 2
compression-algorithm = lz4
swap-priority = 100
"""
        zram_conf_path = Path("/etc/systemd/zram-generator.conf")
        if not zram_conf_path.exists():
            zram_conf_path.write_text(zram_conf)
            run_command(["systemctl", "daemon-reload"])
            run_command(["systemctl", "start", "systemd-zram-generator"])
            print_success("ZRAM настроен через systemd-zram-generator (2x RAM, lz4)")
        else:
            print_success("ZRAM config уже существует")
    else:
        run_command(["apt-get", "install", "-y", "-qq", "zram-tools"])
        zram_conf = """# Managed by mais_server_setup.py
ZRAM_DEVICES=1
ZRAM_SIZE=2048
ZRAM_ALG=lz4
ZRAM_PRIORITY=100
"""
        zram_tools_path = Path("/etc/default/zram-tools")
        if not zram_tools_path.exists() or zram_tools_path.read_text() != zram_conf:
            zram_tools_path.write_text(zram_conf)
            run_command(["systemctl", "enable", "zramswap"])
            run_command(["systemctl", "restart", "zramswap"])
            print_success("ZRAM настроен через zram-tools (2GB, lz4)")
        else:
            print_success("ZRAM config уже существует")

    sysctl_file = Path("/etc/sysctl.d/99-mais-zram.conf")
    if not sysctl_file.exists():
        sysctl_file.write_text("vm.swappiness=60\n")
        run_command(["sysctl", "vm.swappiness=60"])
        print_success("vm.swappiness=60 установлен")
    else:
        print_success("vm.swappiness уже настроен")


# ============================================================
# ШАГ 2: УСТАНОВКА DOCKER
# ============================================================
def install_docker():
    print_header("2. Установка Docker")

    docker_installed = run_command(["docker", "--version"], check=False, capture=True)
    if docker_installed:
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

    run_command(["systemctl", "enable", "docker"], check=False)
    run_command(["systemctl", "start", "docker"], check=False)
    run_command(["usermod", "-aG", "docker", CONFIG['user']], check=False)
    print_success("Docker сервис активен")


def configure_docker_daemon():
    print_step("Настройка Docker Daemon (ротация логов, registry mirror)")

    daemon_path = Path("/etc/docker/daemon.json")
    config = {}
    if daemon_path.exists():
        try:
            config = json.loads(daemon_path.read_text())
        except json.JSONDecodeError:
            print_warning("Повреждён daemon.json, будет перезаписан")

    config["log-driver"] = "json-file"
    config["log-opts"] = {"max-size": "10m", "max-file": "3"}
    config["registry-mirrors"] = ["https://dh-mirror.gitverse.ru"]

    daemon_path.write_text(json.dumps(config, indent=2) + "\n")

    run_command(["systemctl", "restart", "docker"])
    print_success("Docker Daemon настроен")


# ============================================================
# ШАГ 3: ФАЕРВОЛ (nftables drop-in)
# ============================================================
def setup_firewall_rules():
    print_header("3. Настройка фаервола (drop-in)")

    nft_d = Path("/etc/nftables.d")
    nft_d.mkdir(parents=True, exist_ok=True)

    main_conf = Path("/etc/nftables.conf")
    include_line = 'include "/etc/nftables.d/*.nft"'

    if main_conf.exists():
        content = main_conf.read_text()
        if include_line not in content:
            with open(main_conf, 'a') as f:
                f.write(f"\n# App.mais rules\n{include_line}\n")
            print_success("Добавлен include /etc/nftables.d/*.nft в nftables.conf")
    else:
        nftables_conf = f"""#!/usr/sbin/nft -f
flush ruleset

table inet filter {{
    chain input {{
        type filter hook input priority filter; policy drop;
        ct state established,related accept
        iifname "lo" accept
        icmp type {{ echo-reply, echo-request }} accept
        tcp dport 22 accept
    }}
    chain forward {{
        type filter hook forward priority filter; policy drop;
        accept
    }}
    chain output {{
        type filter hook output priority filter; policy accept;
    }}
}}

{include_line}
"""
        main_conf.write_text(nftables_conf)
        print_success("Создан базовый nftables.conf")

    dropin_path = nft_d / "99-mais-app.nft"
    nft_rules = """#!/usr/sbin/nft -f
# Managed by mais_server_setup.py
add rule inet filter input tcp dport { 80, 443 } accept comment "app-mais-http-https"
add rule inet filter input udp dport 443 accept comment "app-mais-quic"
"""
    if not dropin_path.exists():
        dropin_path.write_text(nft_rules)
        print_success("Создан /etc/nftables.d/99-mais-app.nft с правилами 80/443")
    else:
        print_success("Правила фаервола уже существуют")

    existing_rules = run_command(
        ["nft", "list", "chain", "inet", "filter", "input"],
        capture=True, check=False
    )
    if "app-mais-http-https" not in existing_rules and "app-mais" not in existing_rules:
        run_command(["nft", "-f", str(dropin_path)])
        print_success("Правила фаервола применены")
    else:
        print_success("Правила фаервола уже активны")


def configure_sysctl():
    print_step("Настройка sysctl для Docker")

    sysctl_file = Path("/etc/sysctl.d/99-mais-docker.conf")
    if not sysctl_file.exists():
        params = """net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
net.ipv4.conf.all.rp_filter = 0
"""
        sysctl_file.write_text(params)
        run_command(["sysctl", "--system"])
        print_success("Sysctl параметры Docker применены")
    else:
        print_success("Sysctl параметры уже настроены")

    if not os.path.exists("/etc/modules-load.d/br_netfilter.conf"):
        Path("/etc/modules-load.d/br_netfilter.conf").write_text("br_netfilter\n")
        run_command(["modprobe", "br_netfilter"], check=False)
        print_success("br_netfilter модуль настроен")


# ============================================================
# ШАГ 4: СЕКРЕТЫ И БЕЗОПАСНОСТЬ
# ============================================================
def setup_secrets():
    print_header("4. Настройка секретов")

    secrets_dir = Path("/opt/secrets")
    secrets_dir.mkdir(parents=True, exist_ok=True)

    bifrost_env = secrets_dir / "bifrost.env"
    if bifrost_env.exists():
        print_success(f"Файл {bifrost_env} найден")
    else:
        bifrost_env.write_text("# Bifrost API Keys\n# OPENAI_API_KEY=\n# ANTHROPIC_API_KEY=\n")
        bifrost_env.chmod(stat.S_IRUSR | stat.S_IWUSR)
        print_warning(f"Создан пустой шаблон {bifrost_env} — добавьте API-ключи!")


def setup_app_user():
    print_step("Проверка пользователя для контейнеров")

    try:
        pwd.getpwuid(CONFIG['app_uid'])
        print_success(f"Пользователь с UID {CONFIG['app_uid']} существует")
    except KeyError:
        run_command([
            "useradd", "-u", str(CONFIG['app_uid']),
            "-M", "-s", "/sbin/nologin", "appuser"
        ])
        print_success(f"Создан системный пользователь appuser (UID {CONFIG['app_uid']})")


# ============================================================
# ШАГ 5: СТРУКТУРА ПАПОК
# ============================================================
def setup_directories():
    print_header("5. Создание структуры папок")

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
# ШАГ 6: DOCKER СЕТИ (идемпотентно)
# ============================================================
def setup_networks():
    print_header("6. Создание сетей")

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
# ШАГ 7: GHOST
# ============================================================
def setup_ghost():
    print_header("7. Настройка Ghost")

    ghost_dir = Path(f"{CONFIG['docker_dir']}/ghost")
    ghost_dir.mkdir(parents=True, exist_ok=True)

    compose_path = ghost_dir / "compose.yml"
    write_config(str(compose_path), "ghost-compose.yml.j2", CONFIG)

    if container_exists(CONFIG['container_ghost']):
        print_success(f"Контейнер {CONFIG['container_ghost']} уже существует, перезапуск")
        os.chdir(str(ghost_dir))
        run_command(["docker", "compose", "up", "-d"])
    else:
        os.chdir(str(ghost_dir))
        run_command(["docker", "compose", "up", "-d"])
        wait_for_container(CONFIG['container_ghost'], timeout=90)


# ============================================================
# ШАГ 8: BIFROST
# ============================================================
def setup_bifrost():
    print_header("8. Настройка Bifrost")

    bifrost_dir = Path(f"{CONFIG['docker_dir']}/bifrost")
    bifrost_dir.mkdir(parents=True, exist_ok=True)

    compose_path = bifrost_dir / "compose.yml"
    write_config(str(compose_path), "bifrost-compose.yml.j2", CONFIG)

    if container_exists(CONFIG['container_bifrost']):
        print_success(f"Контейнер {CONFIG['container_bifrost']} уже существует, перезапуск")
        os.chdir(str(bifrost_dir))
        run_command(["docker", "compose", "up", "-d"])
    else:
        os.chdir(str(bifrost_dir))
        run_command(["docker", "compose", "up", "-d"])
        wait_for_container(CONFIG['container_bifrost'], timeout=60)


# ============================================================
# ШАГ 9: CADDY
# ============================================================
def setup_caddy():
    print_header("9. Настройка Caddy")

    for domain in [CONFIG['domain_main'], CONFIG['domain_app']]:
        if not check_dns(domain):
            print_warning(f"DNS не настроен для {domain} — Caddy не получит SSL!")
            print_warning("Настройте A-запись и перезапустите: cd /opt/docker/caddy && docker compose restart")

    caddy_dir = Path(f"{CONFIG['docker_dir']}/caddy")
    caddy_dir.mkdir(parents=True, exist_ok=True)

    write_config(str(caddy_dir / "compose.yml"), "caddy-compose.yml.j2", CONFIG)
    write_config(str(caddy_dir / "Caddyfile"), "Caddyfile.j2", CONFIG)

    if container_exists(CONFIG['container_caddy']):
        print_success(f"Контейнер {CONFIG['container_caddy']} уже существует, перезапуск")
        os.chdir(str(caddy_dir))
        run_command(["docker", "compose", "up", "-d"])
    else:
        os.chdir(str(caddy_dir))
        run_command(["docker", "compose", "up", "-d"])
        wait_for_container(CONFIG['container_caddy'], timeout=30)


# ============================================================
# ШАГ 10: ПРОВЕРКА
# ============================================================
def verify():
    print_header("10. Проверка")

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

    zram_status = run_command(["swapon", "--show"], capture=True)
    if "zram" in zram_status:
        print_success("ZRAM активен")
        print(zram_status)
    else:
        print_warning("ZRAM не обнаружен")


# ============================================================
# ШАГ 11: ОБСЛУЖИВАНИЕ (cron)
# ============================================================
def setup_maintenance():
    print_header("11. Обслуживание диска")

    cron_path = Path("/etc/cron.d/mais-docker-prune")
    cron_content = """SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Еженедельная очистка Docker (воскресенье 3:00)
0 3 * * 0 root docker system prune -af --volumes 2>&1 | logger -t mais-docker-prune
"""
    if not cron_path.exists() or cron_path.read_text() != cron_content:
        cron_path.write_text(cron_content)
        cron_path.chmod(0o644)
        print_success("Cron-задача docker prune установлена (каждое воскресенье в 3:00)")
    else:
        print_success("Cron-задача уже существует")


# ============================================================
# ШАГ 12: УТИЛИТЫ УПРАВЛЕНИЯ
# ============================================================
def create_utils():
    print_header("12. Утилиты управления")

    scripts = {
        "status": r"""#!/bin/bash
echo "=== Контейнеры ==="
docker ps --format "table {{.Names}}\t{{.Status}}" --filter "label=project=mais"
echo -e "\n=== Сети ==="
docker network ls --filter "name=mais-"
echo -e "\n=== Volumes ==="
docker volume ls --filter "name=mais-"
""",
        "logs": r"""#!/bin/bash
SERVICE=$1
case $SERVICE in
    ghost) docker logs -f --tail 50 mais-ghost ;;
    bifrost) docker logs -f --tail 50 mais-bifrost ;;
    caddy) docker logs -f --tail 50 mais-caddy ;;
    *) echo "Использование: mais-logs [ghost|bifrost|caddy]" ;;
esac
""",
        "restart": r"""#!/bin/bash
SERVICE=$1
case $SERVICE in
    ghost) docker restart mais-ghost ;;
    bifrost) docker restart mais-bifrost ;;
    caddy) docker restart mais-caddy ;;
    all) docker restart mais-ghost mais-bifrost mais-caddy ;;
    *) echo "Использование: mais-restart [ghost|bifrost|caddy|all]" ;;
esac
""",
    }

    for name, content in scripts.items():
        path = Path("/usr/local/bin") / f"mais-{name}"
        if not path.exists() or path.read_text() != content:
            path.write_text(content)
            path.chmod(0o755)
            print_success(f"Создан: mais-{name}")
        else:
            print_success(f"mais-{name} уже существует")

    print_success("Утилиты управления готовы")


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

{Colors.OKGREEN}Ресурсы:{Colors.ENDC}
  • ZRAM: активен (lz4, 2x RAM)
  • CPU лимиты: Ghost 0.5, Bifrost 0.5, Caddy 0.3
  • RAM лимиты: Ghost 768M, Bifrost 512M, Caddy 128M
  • Log rotation: 10MB × 3 файла
  • Docker prune: еженедельно

{Colors.OKGREEN}Команды:{Colors.ENDC}
  • mais-status   - статус сервисов
  • mais-logs     - логи (mais-logs ghost|bifrost|caddy)
  • mais-restart  - перезапуск

{Colors.WARNING}Следующие шаги:{Colors.ENDC}
  1. Security Groups: открыть TCP 80, 443 от 0.0.0.0/0
  2. DNS: {CONFIG['domain_main']} и {CONFIG['domain_app']} → IP сервера
  3. Добавить API-ключи в /opt/secrets/bifrost.env
  4. Подождать получения SSL-сертификатов Caddy
""")


# ============================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================
def main():
    print_header(f"УСТАНОВКА MAIS AGENCY v{SCRIPT_VERSION}")

    print(f"""
{Colors.OKCYAN}Конфигурация:{Colors.ENDC}
  • Домены: {CONFIG['domain_main']}, {CONFIG['domain_app']}
  • Ghost: {CONFIG['ghost_version']} (CPU: {CONFIG['ghost_cpus']}, RAM: {CONFIG['ghost_memory']})
  • Bifrost: {CONFIG['bifrost_version']} (CPU: {CONFIG['bifrost_cpus']}, RAM: {CONFIG['bifrost_memory']})
  • Caddy: {CONFIG['caddy_version']} (CPU: {CONFIG['caddy_cpus']}, RAM: {CONFIG['caddy_memory']})
  • ZRAM: lz4, 2x RAM, swappiness=60
""")

    wait_for_input("Начать установку?")

    try:
        update_system()
        setup_zram()
        install_docker()
        configure_docker_daemon()
        setup_firewall_rules()
        configure_sysctl()
        setup_secrets()
        setup_app_user()
        setup_directories()
        setup_networks()
        setup_ghost()
        setup_bifrost()
        setup_caddy()
        verify()
        setup_maintenance()
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


# ============================================================
# ОБНОВЛЕНИЕ СИСТЕМЫ (интегрировано, без nftables)
# ============================================================
def update_system():
    print_header("0. Обновление системы")
    run_command(["apt-get", "update", "-qq"])
    run_command([
        "apt-get", "install", "-y", "-qq",
        "curl", "wget", "git", "ca-certificates",
        "gnupg", "python3-jinja2"
    ])
    print_success("Система обновлена")


if __name__ == "__main__":
    check_root()
    main()

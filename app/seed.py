"""Seed built-in blocks, a base image catalogue entry, and (optionally) the
first admin + the test Proxmox connection from environment variables.
"""
from __future__ import annotations

import json

from sqlmodel import select

from .config import settings
from .db import session_scope
from .models import Block, Connection, Image, Network, Template, User
from .security import encrypt, hash_password

UBUNTU_2404_URL = "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"

# Essentials only for v1 — more blocks ship in later releases. Cloud-init templates
# must NOT add their own shell quoting around {placeholders}: render_shell() in
# recipes.py shell-quotes every value (except 'code' fields) so inputs are data.
BUILTIN_BLOCKS = [
    dict(
        key="b-os", name="Base OS Setup", category="OS Setup", icon="settings",
        section="OS Setup", description="timezone + locale",
        input_schema=[
            {"name": "timezone", "type": "text", "default": "UTC", "label": "Timezone"},
            {"name": "locale", "type": "text", "default": "en_US.UTF-8", "label": "Locale"},
        ],
        ansible="- name: Base OS Setup\n  ansible.builtin.timezone:\n    name: {timezone}",
        cloudinit="timedatectl set-timezone {timezone} || true\nlocalectl set-locale LANG={locale} || true",
    ),
    dict(
        key="b-apt", name="Install Packages (apt)", category="Packages", icon="package",
        section="Install", description="apt package list — Debian / Ubuntu",
        input_schema=[
            {"name": "packages", "type": "tags", "default": ["build-essential", "git", "curl", "htop", "jq"], "label": "Packages"},
        ],
        ansible="- name: Install Packages (apt)\n  ansible.builtin.apt:\n    name: {packages_yaml}\n    state: present\n    update_cache: true",
        cloudinit="export DEBIAN_FRONTEND=noninteractive\napt-get update -y\napt-get install -y {packages}",
    ),
    dict(
        # RHEL 8+ / Fedora / Rocky / Alma. dnf refreshes stale metadata on install,
        # but update_cache keeps it explicit. Package NAMES are the user's call —
        # e.g. apt's build-essential has no direct dnf equivalent (use @development-tools),
        # and htop/jq may need EPEL on minimal RHEL.
        key="b-dnf", name="Install Packages (dnf)", category="Packages", icon="package",
        section="Install", description="dnf package list — RHEL 8+ / Fedora / Rocky / Alma",
        input_schema=[
            {"name": "packages", "type": "tags", "default": ["git", "curl", "wget", "tar"], "label": "Packages"},
        ],
        ansible="- name: Install Packages (dnf)\n  ansible.builtin.dnf:\n    name: {packages_yaml}\n    state: present\n    update_cache: true",
        cloudinit="dnf install -y {packages}",
    ),
    dict(
        # Legacy RHEL / CentOS 7 (and Amazon Linux 2) where dnf is absent.
        key="b-yum", name="Install Packages (yum)", category="Packages", icon="package",
        section="Install", description="yum package list — RHEL / CentOS 7",
        input_schema=[
            {"name": "packages", "type": "tags", "default": ["git", "curl", "wget", "tar"], "label": "Packages"},
        ],
        ansible="- name: Install Packages (yum)\n  ansible.builtin.yum:\n    name: {packages_yaml}\n    state: present\n    update_cache: true",
        cloudinit="yum install -y {packages}",
    ),
    dict(
        key="b-script", name="Run Script", category="Scripts", icon="code",
        section="Scripts", description="shell snippet run on the VM",
        input_schema=[
            {"name": "script", "type": "code", "default": "echo hello from goblindock", "label": "Script"},
        ],
        ansible="- name: Run Script\n  ansible.builtin.shell: |\n    {script}",
        cloudinit="{script}",
    ),
    dict(
        key="b-svc", name="Manage Service", category="Services", icon="sliders",
        section="Configure", description="enable / start a systemd unit",
        input_schema=[
            {"name": "service", "type": "text", "default": "docker", "label": "Service"},
            {"name": "enable", "type": "bool", "default": True, "label": "Enable at boot"},
            {"name": "start", "type": "bool", "default": True, "label": "Start now"},
        ],
        ansible="- name: Manage Service\n  ansible.builtin.service:\n    name: {service}\n    enabled: true\n    state: started",
        cloudinit="systemctl enable {service} || true\nsystemctl start {service} || true",
    ),
    dict(
        key="b-docker", name="Docker CE", category="Docker", icon="docker",
        section="Install", description="install engine + compose",
        input_schema=[
            {"name": "compose", "type": "bool", "default": True, "label": "Compose plugin"},
        ],
        ansible="- name: Install Docker CE\n  ansible.builtin.shell: curl -fsSL https://get.docker.com | sh",
        cloudinit="curl -fsSL https://get.docker.com | sh",
    ),
    dict(
        key="b-clean", name="Cleanup & Trim", category="OS Setup", icon="trash",
        section="Cleanup", description="package-cache clean + fstrim (apt/dnf/yum)",
        input_schema=[],
        ansible=(
            "- name: Cleanup & Trim\n"
            "  ansible.builtin.shell: |\n"
            "    if command -v apt-get >/dev/null 2>&1; then apt-get clean || true\n"
            "    elif command -v dnf >/dev/null 2>&1; then dnf clean all || true\n"
            "    elif command -v yum >/dev/null 2>&1; then yum clean all || true; fi\n"
            "    fstrim -av 2>/dev/null || true"
        ),
        cloudinit=(
            "if command -v apt-get >/dev/null 2>&1; then apt-get clean || true\n"
            "elif command -v dnf >/dev/null 2>&1; then dnf clean all || true\n"
            "elif command -v yum >/dev/null 2>&1; then yum clean all || true; fi\n"
            "fstrim -av 2>/dev/null || true"
        ),
    ),

    # ---- extended pre-built blocks (Ansible-module backed, simple inputs) ----
    dict(
        key="b-user", name="User", category="Users / SSH", icon="user",
        section="Accounts",
        description="create a Linux user: groups, SSH key, sudo, password, shell, home",
        input_schema=[
            {"name": "user", "type": "text", "default": "deploy", "label": "Username"},
            {"name": "password", "type": "password", "default": "", "label": "Password · optional", "optional": True},
            {"name": "public_key", "type": "secret", "default": "", "label": "SSH public key · optional", "optional": True},
            {"name": "groups", "type": "tags", "default": [], "label": "Extra groups"},
            {"name": "home", "type": "text", "default": "", "label": "Home directory · optional", "optional": True},
            {"name": "shell", "type": "text", "default": "/bin/bash", "label": "Login shell"},
            {"name": "sudoers", "type": "bool", "default": False, "label": "Add to sudoers"},
            {"name": "nopasswd", "type": "bool", "default": False, "label": "Passwordless sudo (NOPASSWD)"},
            {"name": "ssh_password_login", "type": "bool", "default": False, "label": "Allow SSH password login"},
        ],
        ansible=(
            "- name: User\n"
            "  ansible.builtin.user:\n"
            "    name: {user}\n"
            "    groups: {groups_yamlq}\n"
            "    append: true\n"
            "    create_home: true\n"
            "    shell: {shell}\n"
            "- name: Set home directory\n"
            "  ansible.builtin.user:\n"
            "    name: {user}\n"
            "    home: {home_yamlq}\n"
            "    move_home: true\n"
            "  when: {home_set}\n"
            "- name: Push SSH key\n"
            "  ansible.posix.authorized_key:\n"
            "    user: {user}\n"
            "    state: present\n"
            "    key: {public_key_yamlq}\n"
            "  when: {public_key_set}\n"
            "- name: Set login password\n"
            "  ansible.builtin.shell: echo {user_q}:{password_q} | chpasswd\n"
            "  when: {password_set}\n"
            "- name: Sudoers (passwordless)\n"
            "  ansible.builtin.copy:\n"
            "    dest: /etc/sudoers.d/90-{user}\n"
            "    content: \"{user} ALL=(ALL) NOPASSWD:ALL\\n\"\n"
            "    mode: '0440'\n"
            "    validate: 'visudo -cf %s'\n"
            "  when: {sudoers} and {nopasswd}\n"
            "- name: Sudoers (password required)\n"
            "  ansible.builtin.copy:\n"
            "    dest: /etc/sudoers.d/90-{user}\n"
            "    content: \"{user} ALL=(ALL) ALL\\n\"\n"
            "    mode: '0440'\n"
            "    validate: 'visudo -cf %s'\n"
            "  when: {sudoers} and not {nopasswd}\n"
            "- name: Allow SSH password login\n"
            "  ansible.builtin.shell: |\n"
            "    printf 'PasswordAuthentication yes\\n' > /etc/ssh/sshd_config.d/00-goblindock.conf\n"
            "    systemctl restart ssh || systemctl restart sshd\n"
            "  when: {ssh_password_login}"
        ),
        cloudinit=(
            "id {user} >/dev/null 2>&1 || useradd -m -s {shell} {user}\n"
            "if [ -n {home} ]; then usermod -d {home} -m {user} || true; fi\n"
            "for g in {groups}; do usermod -aG \"$g\" {user} || true; done\n"
            "install -d -m700 /home/{user}/.ssh\n"
            "if [ -n {public_key} ]; then printf '%s\\n' {public_key} >> /home/{user}/.ssh/authorized_keys; "
            "chown -R {user}:{user} /home/{user}/.ssh; chmod 600 /home/{user}/.ssh/authorized_keys; fi\n"
            "if [ -n {password} ]; then echo {user}:{password} | chpasswd; fi\n"
            "if {sudoers}; then\n"
            "  if {nopasswd}; then printf '%s ALL=(ALL) NOPASSWD:ALL\\n' {user} > /etc/sudoers.d/90-{user};\n"
            "  else printf '%s ALL=(ALL) ALL\\n' {user} > /etc/sudoers.d/90-{user}; fi\n"
            "  chmod 440 /etc/sudoers.d/90-{user}; visudo -cf /etc/sudoers.d/90-{user} || rm -f /etc/sudoers.d/90-{user}\n"
            "fi\n"
            "if {ssh_password_login}; then printf 'PasswordAuthentication yes\\n' > /etc/ssh/sshd_config.d/00-goblindock.conf; "
            "systemctl restart ssh || systemctl restart sshd; fi"
        ),
    ),
    dict(
        key="b-authkey", name="SSH Authorized Key", category="Users / SSH", icon="key",
        section="Configure", description="add an authorized SSH key (ansible.posix)",
        input_schema=[
            {"name": "user", "type": "text", "default": "deploy", "label": "User"},
            {"name": "key", "type": "secret", "default": "{{ secrets.TEAM_SSH_PUBKEY }}", "label": "Public key"},
        ],
        ansible=(
            "- name: SSH Authorized Key\n"
            "  ansible.posix.authorized_key:\n"
            "    user: {user}\n"
            "    state: present\n"
            "    key: \"{key}\""
        ),
        cloudinit="install -d -m700 /home/{user}/.ssh\nprintf '%s\\n' {key} >> /home/{user}/.ssh/authorized_keys",
    ),
    dict(
        key="b-writefile", name="Write File", category="Files", icon="file",
        section="Configure", description="write a file with given content",
        input_schema=[
            {"name": "path", "type": "text", "default": "/etc/example.conf", "label": "Path"},
            {"name": "owner", "type": "text", "default": "root", "label": "Owner"},
            {"name": "mode", "type": "text", "default": "0644", "label": "Mode"},
            {"name": "content", "type": "code", "default": "# managed by GoblinDock\n", "label": "Content"},
        ],
        ansible=(
            "- name: Write File\n"
            "  ansible.builtin.copy:\n"
            "    dest: {path}\n"
            "    owner: {owner}\n"
            "    mode: \"{mode}\"\n"
            "    content: |\n"
            "      {content}"
        ),
        cloudinit="cat > {path} <<'GDEOF'\n{content}\nGDEOF\nchown {owner} {path} || true; chmod {mode} {path} || true",
    ),
    dict(
        key="b-mkdir", name="Create Directory", category="Files", icon="file",
        section="Configure", description="ensure a directory exists",
        input_schema=[
            {"name": "path", "type": "text", "default": "/opt/app", "label": "Path"},
            {"name": "owner", "type": "text", "default": "root", "label": "Owner"},
            {"name": "group", "type": "text", "default": "root", "label": "Group"},
            {"name": "mode", "type": "text", "default": "0755", "label": "Mode"},
        ],
        ansible=(
            "- name: Create Directory\n"
            "  ansible.builtin.file:\n"
            "    path: {path}\n"
            "    state: directory\n"
            "    owner: {owner}\n"
            "    group: {group}\n"
            "    mode: \"{mode}\""
        ),
        cloudinit="install -d -m {mode} -o {owner} -g {group} {path}",
    ),
    dict(
        key="b-geturl", name="Download File", category="Files", icon="download",
        section="Install", description="download a file onto the VM",
        input_schema=[
            {"name": "url", "type": "text", "default": "https://example.com/file", "label": "URL"},
            {"name": "dest", "type": "text", "default": "/usr/local/bin/tool", "label": "Destination"},
            {"name": "mode", "type": "text", "default": "0755", "label": "Mode"},
            {"name": "checksum", "type": "text", "default": "", "label": "Checksum (algo:hash)"},
        ],
        ansible=(
            "- name: Download File\n"
            "  ansible.builtin.get_url:\n"
            "    url: {url}\n"
            "    dest: {dest}\n"
            "    mode: \"{mode}\"\n"
            "    checksum: \"{checksum}\""
        ),
        cloudinit="curl -fsSL -o {dest} {url} && chmod {mode} {dest}",
    ),
    dict(
        key="b-git", name="Git Clone", category="Files", icon="code",
        section="Install", description="clone a git repository",
        input_schema=[
            {"name": "repo", "type": "text", "default": "https://github.com/org/repo.git", "label": "Repo URL"},
            {"name": "dest", "type": "text", "default": "/opt/repo", "label": "Destination"},
            {"name": "version", "type": "text", "default": "main", "label": "Branch / tag"},
        ],
        ansible=(
            "- name: Git Clone\n"
            "  ansible.builtin.git:\n"
            "    repo: {repo}\n"
            "    dest: {dest}\n"
            "    version: {version}"
        ),
        cloudinit="git clone --branch {version} {repo} {dest} || true",
    ),
    dict(
        key="b-cron", name="Cron Job", category="Services", icon="history",
        section="Configure", description="schedule a cron job",
        input_schema=[
            {"name": "name", "type": "text", "default": "goblindock job", "label": "Name"},
            {"name": "user", "type": "text", "default": "root", "label": "User"},
            {"name": "minute", "type": "text", "default": "0", "label": "Minute"},
            {"name": "hour", "type": "text", "default": "3", "label": "Hour"},
            {"name": "job", "type": "text", "default": "/usr/local/bin/task.sh", "label": "Command"},
        ],
        ansible=(
            "- name: Cron Job\n"
            "  ansible.builtin.cron:\n"
            "    name: \"{name}\"\n"
            "    user: {user}\n"
            "    minute: \"{minute}\"\n"
            "    hour: \"{hour}\"\n"
            "    job: |-\n"
            "      {job}"
        ),
        cloudinit="( crontab -l 2>/dev/null; echo '{minute} {hour} * * * {job}' ) | crontab -",
    ),
    dict(
        key="b-ufw", name="Firewall Rule", category="Services", icon="shield",
        section="Configure", description="open/close a port — UFW (Debian) or firewalld (RHEL)",
        input_schema=[
            {"name": "port", "type": "text", "default": "22", "label": "Port"},
            {"name": "proto", "type": "select", "options": ["tcp", "udp"], "default": "tcp", "label": "Protocol"},
            {"name": "rule", "type": "select", "options": ["allow", "deny", "limit", "reject"], "default": "allow", "label": "Rule"},
        ],
        # UFW on Debian/Ubuntu, firewalld on RHEL/Oracle. allow/limit -> open the port;
        # deny/reject -> close it (firewalld has no per-port rate-limit, so 'limit' opens).
        ansible=(
            "- name: Firewall Rule\n"
            "  ansible.builtin.shell: |\n"
            "    if command -v ufw >/dev/null 2>&1; then\n"
            "      ufw {rule} {port_q}/{proto} || true\n"
            "    elif command -v firewall-cmd >/dev/null 2>&1; then\n"
            "      case {rule} in\n"
            "        allow|limit) firewall-cmd --permanent --add-port={port_q}/{proto} ;;\n"
            "        deny|reject) firewall-cmd --permanent --remove-port={port_q}/{proto} ;;\n"
            "      esac\n"
            "      firewall-cmd --reload\n"
            "    fi || true"
        ),
        cloudinit=(
            "if command -v ufw >/dev/null 2>&1; then ufw {rule} {port}/{proto} || true\n"
            "elif command -v firewall-cmd >/dev/null 2>&1; then\n"
            "  case {rule} in allow|limit) firewall-cmd --permanent --add-port={port}/{proto};; deny|reject) firewall-cmd --permanent --remove-port={port}/{proto};; esac\n"
            "  firewall-cmd --reload || true\n"
            "fi || true"
        ),
    ),
    dict(
        key="b-hostname", name="Set Hostname", category="OS Setup", icon="server",
        section="OS Setup", description="set the system hostname",
        input_schema=[
            {"name": "hostname", "type": "text", "default": "goblin-vm", "label": "Hostname"},
        ],
        ansible=(
            "- name: Set Hostname\n"
            "  ansible.builtin.hostname:\n"
            "    name: {hostname}"
        ),
        cloudinit="hostnamectl set-hostname {hostname} || true",
    ),
    dict(
        key="b-aptrepo", name="APT Repository", category="Packages", icon="package",
        section="Install", description="add an apt repository (+ optional GPG key)",
        input_schema=[
            {"name": "repo", "type": "text", "default": "deb [arch=amd64] https://example.com/apt stable main", "label": "Repo line"},
            {"name": "filename", "type": "text", "default": "custom", "label": "List filename"},
            {"name": "key_url", "type": "text", "default": "", "label": "GPG key URL (optional)"},
        ],
        ansible=(
            "- name: APT signing key\n"
            "  ansible.builtin.get_url:\n"
            "    url: {key_url}\n"
            "    dest: /etc/apt/trusted.gpg.d/{filename}.asc\n"
            "    mode: \"0644\"\n"
            "  when: \"'{key_url}' != ''\"\n"
            "- name: APT Repository\n"
            "  ansible.builtin.apt_repository:\n"
            "    repo: \"{repo}\"\n"
            "    filename: {filename}\n"
            "    state: present\n"
            "    update_cache: true"
        ),
        cloudinit="echo '{repo}' > /etc/apt/sources.list.d/{filename}.list\napt-get update -y || true",
    ),
    dict(
        key="b-pip", name="Pip Packages", category="Packages", icon="package",
        section="Install", description="install Python packages via pip",
        input_schema=[
            {"name": "packages", "type": "tags", "default": ["requests"], "label": "Packages"},
        ],
        ansible=(
            "- name: Ensure pip\n"
            "  ansible.builtin.shell: |\n"
            "    command -v pip3 >/dev/null 2>&1 && exit 0\n"
            "    if command -v apt-get >/dev/null 2>&1; then apt-get install -y python3-pip\n"
            "    elif command -v dnf >/dev/null 2>&1; then dnf install -y python3-pip\n"
            "    else yum install -y python3-pip; fi\n"
            "- name: Pip Packages\n"
            "  ansible.builtin.pip:\n"
            "    name: {packages_yamlq}\n"
            "    state: present"
        ),
        cloudinit=(
            "if command -v apt-get >/dev/null 2>&1; then apt-get install -y python3-pip\n"
            "elif command -v dnf >/dev/null 2>&1; then dnf install -y python3-pip\n"
            "else yum install -y python3-pip; fi\n"
            "pip3 install {packages}"
        ),
    ),
    dict(
        key="b-sysctl", name="Sysctl Setting", category="OS Setup", icon="sliders",
        section="Configure", description="set a kernel sysctl (ansible.posix)",
        input_schema=[
            {"name": "key", "type": "text", "default": "net.ipv4.ip_forward", "label": "Key"},
            {"name": "value", "type": "text", "default": "1", "label": "Value"},
        ],
        ansible=(
            "- name: Sysctl Setting\n"
            "  ansible.posix.sysctl:\n"
            "    name: {key}\n"
            "    value: \"{value}\"\n"
            "    sysctl_set: true\n"
            "    state: present\n"
            "    reload: true"
        ),
        cloudinit="sysctl -w {key}={value} || true",
    ),
    dict(
        key="b-lineinfile", name="Line in File", category="Files", icon="file",
        section="Configure", description="ensure a line is present in a file",
        input_schema=[
            {"name": "path", "type": "text", "default": "/etc/sysctl.conf", "label": "Path"},
            {"name": "line", "type": "text", "default": "net.ipv4.ip_forward=1", "label": "Line"},
        ],
        ansible=(
            "- name: Line in File\n"
            "  ansible.builtin.lineinfile:\n"
            "    path: {path}\n"
            "    create: true\n"
            "    line: |-\n"
            "      {line}"
        ),
        cloudinit="grep -qxF {line} {path} 2>/dev/null || echo {line} >> {path}",
    ),
    dict(
        key="b-dockerrun", name="Run Docker Container", category="Docker", icon="docker",
        section="Install", description="run a container (community.docker)",
        input_schema=[
            {"name": "name", "type": "text", "default": "app", "label": "Container name"},
            {"name": "image", "type": "text", "default": "nginx:latest", "label": "Image"},
            {"name": "restart", "type": "select", "options": ["no", "on-failure", "always", "unless-stopped"], "default": "unless-stopped", "label": "Restart policy"},
            {"name": "ports", "type": "tags", "default": ["8080:80"], "label": "Ports (host:container)"},
            {"name": "env", "type": "code", "default": "", "label": "Env (KEY: value per line)"},
        ],
        ansible=(
            "- name: Ensure Docker SDK for Python\n"
            "  ansible.builtin.pip:\n"
            "    name: docker\n"
            "- name: Run Docker Container\n"
            "  community.docker.docker_container:\n"
            "    name: {name}\n"
            "    image: {image}\n"
            "    state: started\n"
            "    restart_policy: {restart}\n"
            "    published_ports: {ports_yamlq}\n"
            "    env:\n"
            "      {env}"
        ),
        cloudinit="docker run -d --name {name} --restart {restart} {image}",
    ),
    dict(
        key="b-pgdb", name="PostgreSQL DB + User", category="Databases", icon="sliders",
        section="Install", description="create a database + user (needs PostgreSQL Server; community.postgresql)",
        input_schema=[
            {"name": "db", "type": "text", "default": "appdb", "label": "Database"},
            {"name": "user", "type": "text", "default": "appuser", "label": "User"},
            {"name": "password", "type": "secret", "default": "{{ secrets.PG_PASSWORD }}", "label": "Password"},
        ],
        ansible=(
            "- name: Ensure psycopg2\n"
            "  ansible.builtin.apt:\n"
            "    name: python3-psycopg2\n"
            "    state: present\n"
            "    update_cache: true\n"
            "- name: PostgreSQL user\n"
            "  become_user: postgres\n"
            "  community.postgresql.postgresql_user:\n"
            "    name: {user}\n"
            "    password: \"{password}\"\n"
            "- name: PostgreSQL database\n"
            "  become_user: postgres\n"
            "  community.postgresql.postgresql_db:\n"
            "    name: {db}\n"
            "    owner: {user}"
        ),
        cloudinit="sudo -u postgres createuser {user} 2>/dev/null || true\nsudo -u postgres createdb -O {user} {db} 2>/dev/null || true",
    ),

    # ---- AI coding tools (Claude Code / Codex) ----
    dict(
        key="b-nodejs", name="Node.js (LTS)", category="AI Tools", icon="package",
        section="Install", description="Node.js from NodeSource (Claude Code / Codex need it)",
        input_schema=[
            {"name": "version", "type": "text", "default": "22", "label": "Major version"},
        ],
        # Cross-distro: NodeSource ships separate apt (deb.nodesource.com) and rpm
        # (rpm.nodesource.com) setup scripts — pick by the package manager present so this
        # works on Debian/Ubuntu AND RHEL/Oracle/Fedora. {version} is shell-quoted.
        ansible=(
            "- name: Install Node.js\n"
            "  ansible.builtin.shell: |\n"
            "    if command -v apt-get >/dev/null 2>&1; then\n"
            "      curl -fsSL https://deb.nodesource.com/setup_{version_q}.x | bash - && apt-get install -y nodejs\n"
            "    elif command -v dnf >/dev/null 2>&1; then\n"
            "      curl -fsSL https://rpm.nodesource.com/setup_{version_q}.x | bash - && dnf install -y nodejs\n"
            "    else\n"
            "      curl -fsSL https://rpm.nodesource.com/setup_{version_q}.x | bash - && yum install -y nodejs\n"
            "    fi"
        ),
        cloudinit=(
            "if command -v apt-get >/dev/null 2>&1; then\n"
            "  curl -fsSL https://deb.nodesource.com/setup_{version}.x | bash - && apt-get install -y nodejs\n"
            "elif command -v dnf >/dev/null 2>&1; then\n"
            "  curl -fsSL https://rpm.nodesource.com/setup_{version}.x | bash - && dnf install -y nodejs\n"
            "else\n"
            "  curl -fsSL https://rpm.nodesource.com/setup_{version}.x | bash - && yum install -y nodejs\n"
            "fi"
        ),
    ),
    dict(
        key="b-claudecode", name="Claude Code", category="AI Tools", icon="spark",
        section="Install", description="Anthropic Claude Code CLI (native installer — any Linux, no Node)",
        input_schema=[
            {"name": "user", "type": "text", "default": "goblin", "label": "Install for user"},
        ],
        # Native installer drops a self-contained binary into the user's ~/.local/bin —
        # no Node/npm and distro-agnostic (Debian/Ubuntu, RHEL/Oracle/Fedora, …). The
        # username is the ONLY interpolation and is shell-quoted — {user_q} (shlex) for
        # ansible, and render_shell auto-quotes {user} for cloud-init — so it is treated
        # as data and can't break out of the command. `sudo -u … -H` runs as that user
        # with their real $HOME; the `test -x` guard makes it a no-op once installed.
        ansible=(
            "- name: Install Claude Code\n"
            "  ansible.builtin.shell: |\n"
            "    sudo -u {user_q} -H bash -c 'test -x \"$HOME/.local/bin/claude\" || curl -fsSL https://claude.ai/install.sh | bash'"
        ),
        cloudinit="sudo -u {user} -H bash -c 'test -x \"$HOME/.local/bin/claude\" || curl -fsSL https://claude.ai/install.sh | bash'",
    ),
    dict(
        key="b-codex", name="OpenAI Codex", category="AI Tools", icon="spark",
        section="Install", description="OpenAI Codex CLI (npm global — installs Node if absent)",
        input_schema=[
            {"name": "node_version", "type": "text", "default": "22", "label": "Node version (installed if absent)"},
        ],
        # Codex needs Node/npm. Install it cross-distro (NodeSource apt vs rpm, by package
        # manager) only when npm is missing, then install Codex globally. Version quoted.
        ansible=(
            "- name: Install OpenAI Codex\n"
            "  ansible.builtin.shell: |\n"
            "    if ! command -v npm >/dev/null 2>&1; then\n"
            "      if command -v apt-get >/dev/null 2>&1; then\n"
            "        curl -fsSL https://deb.nodesource.com/setup_{node_version_q}.x | bash - && apt-get install -y nodejs\n"
            "      elif command -v dnf >/dev/null 2>&1; then\n"
            "        curl -fsSL https://rpm.nodesource.com/setup_{node_version_q}.x | bash - && dnf install -y nodejs\n"
            "      else\n"
            "        curl -fsSL https://rpm.nodesource.com/setup_{node_version_q}.x | bash - && yum install -y nodejs\n"
            "      fi\n"
            "    fi\n"
            "    npm install -g @openai/codex"
        ),
        cloudinit=(
            "if ! command -v npm >/dev/null 2>&1; then\n"
            "  if command -v apt-get >/dev/null 2>&1; then\n"
            "    curl -fsSL https://deb.nodesource.com/setup_{node_version}.x | bash - && apt-get install -y nodejs\n"
            "  elif command -v dnf >/dev/null 2>&1; then\n"
            "    curl -fsSL https://rpm.nodesource.com/setup_{node_version}.x | bash - && dnf install -y nodejs\n"
            "  else\n"
            "    curl -fsSL https://rpm.nodesource.com/setup_{node_version}.x | bash - && yum install -y nodejs\n"
            "  fi\n"
            "fi\n"
            "npm install -g @openai/codex"
        ),
    ),
    dict(
        key="b-claudemd", name="Global CLAUDE.md", category="AI Tools", icon="file",
        section="Configure", description="write ~/.claude/CLAUDE.md (global Claude Code instructions)",
        input_schema=[
            {"name": "user", "type": "text", "default": "goblin", "label": "User"},
            {"name": "content", "type": "code",
             "default": "# Global Claude Code instructions (~/.claude/CLAUDE.md)\n# Applies to every Claude Code session for this user.\n\nBefore answering, reason step by step and verify the answer against all constraints in the request.\n",
             "label": "CLAUDE.md content"},
        ],
        ansible=(
            "- name: Ensure ~/.claude exists\n"
            "  ansible.builtin.file:\n"
            "    path: /home/{user}/.claude\n"
            "    state: directory\n"
            "    owner: {user}\n"
            "    group: {user}\n"
            "    mode: \"0755\"\n"
            "- name: Global CLAUDE.md\n"
            "  ansible.builtin.copy:\n"
            "    dest: /home/{user}/.claude/CLAUDE.md\n"
            "    owner: {user}\n"
            "    mode: \"0644\"\n"
            "    content: |\n"
            "      {content}"
        ),
        cloudinit="install -d -o {user} -g {user} /home/{user}/.claude\ncat > /home/{user}/.claude/CLAUDE.md <<'GDEOF'\n{content}\nGDEOF\nchown {user}:{user} /home/{user}/.claude/CLAUDE.md",
    ),
    dict(
        key="b-claudesettings", name="Claude settings.json", category="AI Tools", icon="settings",
        section="Configure", description="write ~/.claude/settings.json (Claude Code settings)",
        input_schema=[
            {"name": "user", "type": "text", "default": "goblin", "label": "User"},
            {"name": "content", "type": "code", "default": "{\n  \"includeCoAuthoredBy\": false\n}\n", "label": "settings.json"},
        ],
        ansible=(
            "- name: Ensure ~/.claude exists\n"
            "  ansible.builtin.file:\n"
            "    path: /home/{user}/.claude\n"
            "    state: directory\n"
            "    owner: {user}\n"
            "    group: {user}\n"
            "    mode: \"0755\"\n"
            "- name: Claude settings.json\n"
            "  ansible.builtin.copy:\n"
            "    dest: /home/{user}/.claude/settings.json\n"
            "    owner: {user}\n"
            "    mode: \"0644\"\n"
            "    content: |\n"
            "      {content}"
        ),
        cloudinit="install -d -o {user} -g {user} /home/{user}/.claude\ncat > /home/{user}/.claude/settings.json <<'GDEOF'\n{content}\nGDEOF\nchown {user}:{user} /home/{user}/.claude/settings.json",
    ),
    dict(
        key="b-claudemcp", name="Claude MCP Server", category="AI Tools", icon="blocks",
        section="Configure", description="register an MCP server for Claude Code (user scope)",
        input_schema=[
            {"name": "user", "type": "text", "default": "goblin", "label": "User"},
            {"name": "name", "type": "text", "default": "context7", "label": "Server name"},
            # 'command' is an arbitrary launcher command (program + args after `--`), so it
            # is intentionally code (raw shell), like the Run Script body — shell-quoting it
            # would collapse the multi-word command into a single argument and break it.
            {"name": "command", "type": "code", "default": "npx -y @upstash/context7-mcp", "label": "Command"},
        ],
        ansible=(
            "- name: Add Claude MCP server\n"
            "  become_user: {user}\n"
            "  environment:\n"
            "    HOME: /home/{user}\n"
            "  ansible.builtin.shell: claude mcp add {name_q} -s user -- {command} || true"
        ),
        cloudinit="sudo -u {user} -H claude mcp add {name} -s user -- {command} || true",
    ),

    dict(
        key="b-conpw", name="Console Password", category="Users / SSH", icon="key",
        section="Accounts", description="set a password so a user can log in at the console (serial/VNC)",
        input_schema=[
            {"name": "user", "type": "text", "default": "goblin", "label": "User"},
            {"name": "password", "type": "secret", "default": "{{ secrets.CONSOLE_PASSWORD }}", "label": "Password"},
        ],
        ansible=(
            "- name: Console Password\n"
            "  ansible.builtin.shell: |\n"
            "    echo {user_q}:{password_q} | chpasswd\n"
            "    passwd -u {user_q} 2>/dev/null || true"
        ),
        cloudinit="echo {user}:{password} | chpasswd\npasswd -u {user} 2>/dev/null || true",
    ),

    # ---- networking ----
    dict(
        key="b-tailscale", name="Tailscale", category="Networking", icon="globe",
        section="Install", description="join your tailnet at deploy (auth key)",
        input_schema=[
            {"name": "authkey", "type": "secret", "default": "{{ secrets.TAILSCALE_AUTHKEY }}", "label": "Auth key"},
            {"name": "tailscale_ssh", "type": "bool", "default": False, "label": "Enable Tailscale SSH"},
            # extra `tailscale up` flags (e.g. --advertise-tags) — intentionally raw, like Run Script
            {"name": "args", "type": "code", "default": "", "label": "Extra `tailscale up` flags · optional"},
        ],
        ansible=(
            "- name: Install Tailscale\n"
            "  ansible.builtin.shell: curl -fsSL https://tailscale.com/install.sh | sh\n"
            "- name: Join tailnet\n"
            "  ansible.builtin.shell: |\n"
            "    if {tailscale_ssh}; then tailscale up --auth-key={authkey_q} --ssh {args}; else tailscale up --auth-key={authkey_q} {args}; fi\n"
            "  when: {authkey_set}"
        ),
        cloudinit=(
            "curl -fsSL https://tailscale.com/install.sh | sh\n"
            "if {tailscale_ssh}; then tailscale up --auth-key={authkey} --ssh; else tailscale up --auth-key={authkey}; fi"
        ),
    ),
    dict(
        key="b-k3s", name="K3s Node", category="Networking", icon="network",
        section="Install", description="single-node k3s server, or an agent joining a cluster",
        input_schema=[
            {"name": "role", "type": "select", "options": ["server", "agent"], "default": "server", "label": "Role"},
            {"name": "server_url", "type": "text", "default": "", "label": "Server URL (agent) · https://host:6443", "optional": True},
            {"name": "token", "type": "secret", "default": "", "label": "Join token (agent / extra servers)", "optional": True},
        ],
        ansible=(
            "- name: Install K3s\n"
            "  ansible.builtin.shell: |\n"
            "    if [ {role_q} = agent ]; then\n"
            "      curl -sfL https://get.k3s.io | K3S_URL={server_url_q} K3S_TOKEN={token_q} sh -s - agent\n"
            "    elif [ -n {token_q} ]; then\n"
            "      curl -sfL https://get.k3s.io | K3S_TOKEN={token_q} sh -s - server\n"
            "    else\n"
            "      curl -sfL https://get.k3s.io | sh -s - server\n"
            "    fi"
        ),
        cloudinit="curl -sfL https://get.k3s.io | sh -s - server",
    ),

    # ---- security ----
    dict(
        key="b-sshharden", name="SSH Hardening", category="Security", icon="shield",
        section="Configure", description="root login / password auth / port / allowed users",
        input_schema=[
            {"name": "permit_root", "type": "bool", "default": False, "label": "Permit root login"},
            {"name": "password_auth", "type": "bool", "default": False, "label": "Allow password auth"},
            {"name": "port", "type": "text", "default": "22", "label": "SSH port"},
            {"name": "allow_users", "type": "text", "default": "", "label": "AllowUsers (space-separated, empty = all)", "optional": True},
        ],
        # 00-goblindock-hardening sorts before 00-goblindock.conf ('-' < '.'), so on a
        # conflict with the password-login toggle of the User block the hardening wins.
        ansible=(
            "- name: SSH Hardening\n"
            "  ansible.builtin.shell: |\n"
            "    conf=/etc/ssh/sshd_config.d/00-goblindock-hardening.conf\n"
            "    : > \"$conf\"\n"
            "    if {permit_root}; then echo 'PermitRootLogin yes' >> \"$conf\"; else echo 'PermitRootLogin no' >> \"$conf\"; fi\n"
            "    if {password_auth}; then echo 'PasswordAuthentication yes' >> \"$conf\"; else echo 'PasswordAuthentication no' >> \"$conf\"; fi\n"
            "    echo Port {port_q} >> \"$conf\"\n"
            "    if [ -n {allow_users_q} ]; then echo AllowUsers {allow_users_q} >> \"$conf\"; fi\n"
            "    sshd -t\n"
            "    systemctl restart ssh || systemctl restart sshd"
        ),
        cloudinit=(
            "conf=/etc/ssh/sshd_config.d/00-goblindock-hardening.conf\n"
            "if {permit_root}; then echo 'PermitRootLogin yes' > \"$conf\"; else echo 'PermitRootLogin no' > \"$conf\"; fi\n"
            "if {password_auth}; then echo 'PasswordAuthentication yes' >> \"$conf\"; else echo 'PasswordAuthentication no' >> \"$conf\"; fi\n"
            "echo Port {port} >> \"$conf\"\n"
            "if [ -n {allow_users} ]; then echo AllowUsers {allow_users} >> \"$conf\"; fi\n"
            "sshd -t && systemctl restart ssh || systemctl restart sshd"
        ),
    ),
    dict(
        key="b-fail2ban", name="Fail2ban", category="Security", icon="shield",
        section="Install", description="brute-force protection with an sshd jail",
        input_schema=[
            {"name": "bantime", "type": "text", "default": "1h", "label": "Ban time"},
            {"name": "maxretry", "type": "text", "default": "5", "label": "Max retries"},
            {"name": "extra", "type": "code", "default": "", "label": "Extra jail.local lines · optional"},
        ],
        ansible=(
            "- name: Install Fail2ban\n"
            "  ansible.builtin.shell: |\n"
            "    if command -v apt-get >/dev/null 2>&1; then apt-get install -y fail2ban\n"
            "    elif command -v dnf >/dev/null 2>&1; then dnf install -y epel-release || true; dnf install -y fail2ban\n"
            "    else yum install -y epel-release || true; yum install -y fail2ban; fi\n"
            "- name: Fail2ban sshd jail\n"
            "  ansible.builtin.copy:\n"
            "    dest: /etc/fail2ban/jail.d/goblindock.local\n"
            "    mode: \"0644\"\n"
            "    content: |\n"
            "      [sshd]\n"
            "      enabled = true\n"
            "      backend = systemd\n"
            "      bantime = {bantime}\n"
            "      maxretry = {maxretry}\n"
            "      {extra}\n"
            "- name: Enable Fail2ban\n"
            "  ansible.builtin.service:\n"
            "    name: fail2ban\n"
            "    enabled: true\n"
            "    state: restarted"
        ),
        cloudinit=(
            "if command -v apt-get >/dev/null 2>&1; then apt-get install -y fail2ban\n"
            "elif command -v dnf >/dev/null 2>&1; then dnf install -y epel-release || true; dnf install -y fail2ban\n"
            "else yum install -y epel-release || true; yum install -y fail2ban; fi\n"
            "printf '[sshd]\\nenabled = true\\nbackend = systemd\\nbantime = %s\\nmaxretry = %s\\n' {bantime} {maxretry} > /etc/fail2ban/jail.d/goblindock.local\n"
            "systemctl enable --now fail2ban && systemctl restart fail2ban"
        ),
    ),
    dict(
        key="b-cacert", name="Trust Internal CA", category="Security", icon="lock",
        section="Configure", description="install a CA certificate into the system trust store",
        input_schema=[
            {"name": "name", "type": "text", "default": "internal-ca", "label": "Name (filename)"},
            {"name": "pem", "type": "code", "default": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----", "label": "CA certificate (PEM)"},
        ],
        # Debian trusts /usr/local/share/ca-certificates + update-ca-certificates; RHEL/Oracle
        # trusts /etc/pki/ca-trust/source/anchors + update-ca-trust. Stage via the copy module
        # (clean multi-line PEM), then a shell task installs it into whichever exists.
        ansible=(
            "- name: Stage CA certificate\n"
            "  ansible.builtin.copy:\n"
            "    dest: /tmp/gd-{name}.crt\n"
            "    mode: \"0644\"\n"
            "    content: |\n"
            "      {pem}\n"
            "- name: Install CA into the trust store\n"
            "  ansible.builtin.shell: |\n"
            "    if command -v update-ca-trust >/dev/null 2>&1; then\n"
            "      install -m 0644 /tmp/gd-{name_q}.crt /etc/pki/ca-trust/source/anchors/{name_q}.crt && update-ca-trust extract\n"
            "    else\n"
            "      install -m 0644 /tmp/gd-{name_q}.crt /usr/local/share/ca-certificates/{name_q}.crt && update-ca-certificates\n"
            "    fi\n"
            "    rm -f /tmp/gd-{name_q}.crt"
        ),
        cloudinit=(
            "if command -v update-ca-trust >/dev/null 2>&1; then dest=/etc/pki/ca-trust/source/anchors/{name}.crt; else dest=/usr/local/share/ca-certificates/{name}.crt; fi\n"
            "cat > \"$dest\" <<'GDEOF'\n{pem}\nGDEOF\n"
            "if command -v update-ca-trust >/dev/null 2>&1; then update-ca-trust extract; else update-ca-certificates; fi"
        ),
    ),
    dict(
        key="b-autoupdates", name="Unattended Upgrades", category="Security", icon="refresh",
        section="Configure", description="automatic security updates (optional auto-reboot)",
        input_schema=[
            {"name": "auto_reboot", "type": "bool", "default": False, "label": "Auto-reboot when required"},
            {"name": "reboot_time", "type": "text", "default": "03:00", "label": "Reboot time"},
        ],
        ansible=(
            "- name: Automatic security updates\n"
            "  ansible.builtin.shell: |\n"
            "    if command -v apt-get >/dev/null 2>&1; then\n"
            "      apt-get install -y unattended-upgrades\n"
            "      printf 'APT::Periodic::Update-Package-Lists \"1\";\\nAPT::Periodic::Unattended-Upgrade \"1\";\\n' > /etc/apt/apt.conf.d/20auto-upgrades\n"
            "      if {auto_reboot}; then printf 'Unattended-Upgrade::Automatic-Reboot \"true\";\\nUnattended-Upgrade::Automatic-Reboot-Time \"{reboot_time}\";\\n' > /etc/apt/apt.conf.d/51goblindock-reboot; fi\n"
            "    else\n"
            "      (command -v dnf >/dev/null 2>&1 && dnf install -y dnf-automatic) || yum install -y dnf-automatic\n"
            "      sed -i 's/^apply_updates = .*/apply_updates = yes/' /etc/dnf/automatic.conf 2>/dev/null || true\n"
            "      if {auto_reboot}; then sed -i 's/^reboot = .*/reboot = when-needed/' /etc/dnf/automatic.conf 2>/dev/null || true; fi\n"
            "      systemctl enable --now dnf-automatic.timer\n"
            "    fi"
        ),
        cloudinit=(
            "if command -v apt-get >/dev/null 2>&1; then\n"
            "  apt-get install -y unattended-upgrades\n"
            "  printf 'APT::Periodic::Update-Package-Lists \"1\";\\nAPT::Periodic::Unattended-Upgrade \"1\";\\n' > /etc/apt/apt.conf.d/20auto-upgrades\n"
            "  if {auto_reboot}; then printf 'Unattended-Upgrade::Automatic-Reboot \"true\";\\nUnattended-Upgrade::Automatic-Reboot-Time \"%s\";\\n' {reboot_time} > /etc/apt/apt.conf.d/51goblindock-reboot; fi\n"
            "else\n"
            "  (command -v dnf >/dev/null 2>&1 && dnf install -y dnf-automatic) || yum install -y dnf-automatic\n"
            "  sed -i 's/^apply_updates = .*/apply_updates = yes/' /etc/dnf/automatic.conf 2>/dev/null || true\n"
            "  if {auto_reboot}; then sed -i 's/^reboot = .*/reboot = when-needed/' /etc/dnf/automatic.conf 2>/dev/null || true; fi\n"
            "  systemctl enable --now dnf-automatic.timer\n"
            "fi"
        ),
    ),

    # ---- docker extras (pair with the Docker CE block) ----
    dict(
        key="b-compose", name="Docker Compose Stack", category="Docker", icon="docker",
        section="Install", description="deploy a compose stack to /opt/<name> (needs Docker CE)",
        input_schema=[
            {"name": "name", "type": "text", "default": "app", "label": "Stack name"},
            {"name": "compose", "type": "code",
             "default": "services:\n  web:\n    image: nginx:latest\n    ports:\n      - \"8080:80\"\n    restart: unless-stopped\n",
             "label": "compose.yml"},
            {"name": "env", "type": "code", "default": "", "label": ".env · optional"},
        ],
        ansible=(
            "- name: Stack directory\n"
            "  ansible.builtin.file:\n"
            "    path: /opt/{name}\n"
            "    state: directory\n"
            "    mode: \"0755\"\n"
            "- name: Write compose.yml\n"
            "  ansible.builtin.copy:\n"
            "    dest: /opt/{name}/compose.yml\n"
            "    mode: \"0644\"\n"
            "    content: |\n"
            "      {compose}\n"
            "- name: Write .env\n"
            "  ansible.builtin.copy:\n"
            "    dest: /opt/{name}/.env\n"
            "    mode: \"0600\"\n"
            "    content: |\n"
            "      {env}\n"
            "  when: {env_set}\n"
            "- name: Compose up\n"
            "  ansible.builtin.shell: docker compose up -d\n"
            "  args:\n"
            "    chdir: /opt/{name}"
        ),
        cloudinit=(
            "install -d /opt/{name}\n"
            "cat > /opt/{name}/compose.yml <<'GDEOF'\n{compose}\nGDEOF\n"
            "cd /opt/{name} && docker compose up -d"
        ),
    ),
    dict(
        key="b-watchtower", name="Watchtower", category="Docker", icon="refresh",
        section="Install", description="auto-update running containers (needs Docker CE)",
        input_schema=[
            {"name": "interval", "type": "text", "default": "86400", "label": "Poll interval (seconds)"},
            {"name": "cleanup", "type": "bool", "default": True, "label": "Remove old images"},
        ],
        ansible=(
            "- name: Run Watchtower\n"
            "  ansible.builtin.shell: |\n"
            "    docker rm -f watchtower 2>/dev/null || true\n"
            "    extra=''\n"
            "    if {cleanup}; then extra=--cleanup; fi\n"
            "    docker run -d --name watchtower --restart unless-stopped -v /var/run/docker.sock:/var/run/docker.sock containrrr/watchtower --interval {interval_q} $extra"
        ),
        cloudinit=(
            "docker rm -f watchtower 2>/dev/null || true\n"
            "docker run -d --name watchtower --restart unless-stopped -v /var/run/docker.sock:/var/run/docker.sock containrrr/watchtower --interval {interval}"
        ),
    ),
    dict(
        key="b-portaineragent", name="Portainer Agent", category="Docker", icon="docker",
        section="Install", description="let an existing Portainer manage this VM (needs Docker CE)",
        input_schema=[
            {"name": "port", "type": "text", "default": "9001", "label": "Agent port"},
        ],
        ansible=(
            "- name: Run Portainer Agent\n"
            "  ansible.builtin.shell: |\n"
            "    docker rm -f portainer_agent 2>/dev/null || true\n"
            "    docker run -d --name portainer_agent --restart always -p {port_q}:9001 -v /var/run/docker.sock:/var/run/docker.sock -v /var/lib/docker/volumes:/var/lib/docker/volumes portainer/agent:latest"
        ),
        cloudinit="docker run -d --name portainer_agent --restart always -p {port}:9001 -v /var/run/docker.sock:/var/run/docker.sock -v /var/lib/docker/volumes:/var/lib/docker/volumes portainer/agent:latest",
    ),

    # ---- storage / databases ----
    dict(
        key="b-mountshare", name="Mount Network Share", category="Files", icon="disk",
        section="Configure", description="mount an NFS or CIFS share via fstab (ansible.posix)",
        input_schema=[
            {"name": "fstype", "type": "select", "options": ["nfs", "cifs"], "default": "nfs", "label": "Type"},
            {"name": "src", "type": "text", "default": "192.168.1.10:/export/media", "label": "Share (server:/path or //server/share)"},
            {"name": "mountpoint", "type": "text", "default": "/mnt/share", "label": "Mountpoint"},
            {"name": "opts", "type": "text", "default": "defaults", "label": "Options (cifs: username=…,password=…)"},
        ],
        ansible=(
            "- name: Install mount tools\n"
            "  ansible.builtin.shell: |\n"
            "    if command -v apt-get >/dev/null 2>&1; then apt-get install -y nfs-common cifs-utils\n"
            "    elif command -v dnf >/dev/null 2>&1; then dnf install -y nfs-utils cifs-utils\n"
            "    else yum install -y nfs-utils cifs-utils; fi\n"
            "- name: Mount Network Share\n"
            "  ansible.posix.mount:\n"
            "    path: {mountpoint}\n"
            "    src: \"{src}\"\n"
            "    fstype: {fstype}\n"
            "    opts: \"{opts}\"\n"
            "    state: mounted"
        ),
        cloudinit=(
            "if command -v apt-get >/dev/null 2>&1; then apt-get install -y nfs-common cifs-utils\n"
            "elif command -v dnf >/dev/null 2>&1; then dnf install -y nfs-utils cifs-utils\n"
            "else yum install -y nfs-utils cifs-utils; fi\n"
            "mkdir -p {mountpoint}\n"
            "grep -qF {src} /etc/fstab || printf '%s %s %s %s 0 0\\n' {src} {mountpoint} {fstype} {opts} >> /etc/fstab\n"
            "mount -a || true"
        ),
    ),
    dict(
        key="b-swap", name="Swap File", category="OS Setup", icon="ram",
        section="OS Setup", description="swap file at first boot — helps small VMs survive heavy installs",
        input_schema=[
            {"name": "size_gb", "type": "text", "default": "2", "label": "Size (GB)"},
            {"name": "swappiness", "type": "text", "default": "10", "label": "Swappiness"},
        ],
        ansible=(
            "- name: Swap File\n"
            "  ansible.builtin.shell: |\n"
            "    [ -f /swapfile ] || fallocate -l {size_gb_q}G /swapfile\n"
            "    chmod 600 /swapfile\n"
            "    mkswap /swapfile 2>/dev/null || true\n"
            "    swapon /swapfile 2>/dev/null || true\n"
            "    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab\n"
            "    sysctl -w vm.swappiness={swappiness_q} || true\n"
            "    echo vm.swappiness={swappiness_q} > /etc/sysctl.d/99-goblindock-swap.conf"
        ),
        cloudinit=(
            "[ -f /swapfile ] || fallocate -l {size_gb}G /swapfile\n"
            "chmod 600 /swapfile\n"
            "mkswap /swapfile 2>/dev/null || true\n"
            "swapon /swapfile 2>/dev/null || true\n"
            "grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab\n"
            "sysctl -w vm.swappiness={swappiness} || true\n"
            "echo vm.swappiness={swappiness} > /etc/sysctl.d/99-goblindock-swap.conf"
        ),
    ),
    dict(
        key="b-mariadb", name="MariaDB Server", category="Databases", icon="box",
        section="Install", description="install MariaDB + optional database & user",
        input_schema=[
            {"name": "database", "type": "text", "default": "appdb", "label": "Database · optional", "optional": True},
            {"name": "user", "type": "text", "default": "appuser", "label": "User · optional", "optional": True},
            {"name": "password", "type": "password", "default": "", "label": "User password", "optional": True},
            {"name": "lan", "type": "bool", "default": False, "label": "Listen on LAN (default localhost)"},
        ],
        # no community.mysql collection in the image — root works over the unix socket
        # on Debian/Ubuntu, so plain `mysql -e` does the provisioning.
        ansible=(
            "- name: Install MariaDB\n"
            "  ansible.builtin.shell: |\n"
            "    if command -v apt-get >/dev/null 2>&1; then apt-get install -y mariadb-server\n"
            "    elif command -v dnf >/dev/null 2>&1; then dnf install -y mariadb-server\n"
            "    else yum install -y mariadb-server; fi\n"
            "- name: Enable MariaDB\n"
            "  ansible.builtin.service:\n"
            "    name: mariadb\n"
            "    enabled: true\n"
            "    state: started\n"
            "- name: Listen on LAN\n"
            "  ansible.builtin.shell: |\n"
            "    if [ -d /etc/mysql/mariadb.conf.d ]; then d=/etc/mysql/mariadb.conf.d; else d=/etc/my.cnf.d; fi\n"
            "    printf '[mysqld]\\nbind-address = 0.0.0.0\\n' > \"$d/99-goblindock.cnf\"\n"
            "    systemctl restart mariadb\n"
            "  when: {lan}\n"
            "- name: Create database\n"
            "  ansible.builtin.shell: mysql -e 'CREATE DATABASE IF NOT EXISTS '{database_q}\n"
            "  when: {database_set}\n"
            "- name: Create user + grant\n"
            "  ansible.builtin.shell: |\n"
            "    mysql -e \"CREATE USER IF NOT EXISTS '{user}'@'%' IDENTIFIED BY {password_q}\"\n"
            "    mysql -e \"GRANT ALL PRIVILEGES ON \"{database_q}\".* TO '{user}'@'%'\"\n"
            "    mysql -e \"FLUSH PRIVILEGES\"\n"
            "  when: {password_set}"
        ),
        cloudinit=(
            "if command -v apt-get >/dev/null 2>&1; then apt-get install -y mariadb-server\n"
            "elif command -v dnf >/dev/null 2>&1; then dnf install -y mariadb-server\n"
            "else yum install -y mariadb-server; fi\n"
            "systemctl enable --now mariadb"
        ),
    ),
    dict(
        key="b-pgserver", name="PostgreSQL Server", category="Databases", icon="box",
        section="Install", description="install PostgreSQL (pair with PostgreSQL DB + User)",
        input_schema=[
            {"name": "lan", "type": "bool", "default": False, "label": "Listen on LAN + allow remote auth"},
            {"name": "allow_cidr", "type": "text", "default": "192.168.0.0/16", "label": "Allowed CIDR (LAN mode)"},
        ],
        ansible=(
            "- name: Install PostgreSQL\n"
            "  ansible.builtin.shell: |\n"
            "    if command -v apt-get >/dev/null 2>&1; then\n"
            "      apt-get install -y postgresql postgresql-contrib\n"
            "    else\n"
            "      (command -v dnf >/dev/null 2>&1 && dnf install -y postgresql-server postgresql-contrib) || yum install -y postgresql-server postgresql-contrib\n"
            "      [ -f /var/lib/pgsql/data/PG_VERSION ] || postgresql-setup --initdb || /usr/bin/postgresql-setup initdb || true\n"
            "    fi\n"
            "    systemctl enable --now postgresql\n"
            "- name: Listen on LAN\n"
            "  ansible.builtin.shell: |\n"
            "    sudo -u postgres psql -c \"ALTER SYSTEM SET listen_addresses = '*'\"\n"
            "    hba=$(sudo -u postgres psql -tAc 'show hba_file')\n"
            "    grep -q goblindock \"$hba\" || echo 'host all all '{allow_cidr_q}' scram-sha-256 # goblindock' >> \"$hba\"\n"
            "    systemctl restart postgresql\n"
            "  when: {lan}"
        ),
        cloudinit=(
            "if command -v apt-get >/dev/null 2>&1; then apt-get install -y postgresql postgresql-contrib\n"
            "else\n"
            "  (command -v dnf >/dev/null 2>&1 && dnf install -y postgresql-server postgresql-contrib) || yum install -y postgresql-server postgresql-contrib\n"
            "  [ -f /var/lib/pgsql/data/PG_VERSION ] || postgresql-setup --initdb || /usr/bin/postgresql-setup initdb || true\n"
            "fi\n"
            "systemctl enable --now postgresql"
        ),
    ),
    dict(
        key="b-redis", name="Redis Server", category="Databases", icon="box",
        section="Install", description="install Redis (optional password / LAN / memory cap)",
        input_schema=[
            {"name": "password", "type": "password", "default": "", "label": "Password · optional", "optional": True},
            {"name": "lan", "type": "bool", "default": False, "label": "Listen on LAN (default localhost)"},
            {"name": "maxmemory", "type": "text", "default": "", "label": "Max memory (e.g. 256mb) · optional", "optional": True},
        ],
        ansible=(
            "- name: Install Redis\n"
            "  ansible.builtin.shell: |\n"
            "    if command -v apt-get >/dev/null 2>&1; then apt-get install -y redis-server\n"
            "    elif command -v dnf >/dev/null 2>&1; then dnf install -y redis\n"
            "    else yum install -y redis; fi\n"
            "- name: Configure Redis\n"
            "  ansible.builtin.shell: |\n"
            "    conf=/etc/redis/redis.conf; [ -f \"$conf\" ] || conf=/etc/redis.conf\n"
            "    systemctl list-unit-files 2>/dev/null | grep -q '^redis-server' && svc=redis-server || svc=redis\n"
            "    if {lan}; then sed -i 's/^bind .*/bind 0.0.0.0 ::1/' \"$conf\"; sed -i 's/^protected-mode yes/protected-mode no/' \"$conf\"; fi\n"
            "    if [ -n {password_q} ]; then printf 'requirepass %s\\n' {password_q} >> \"$conf\"; fi\n"
            "    if [ -n {maxmemory_q} ]; then printf 'maxmemory %s\\nmaxmemory-policy allkeys-lru\\n' {maxmemory_q} >> \"$conf\"; fi\n"
            "    systemctl enable \"$svc\"\n"
            "    systemctl restart \"$svc\""
        ),
        cloudinit=(
            "if command -v apt-get >/dev/null 2>&1; then apt-get install -y redis-server; svc=redis-server\n"
            "elif command -v dnf >/dev/null 2>&1; then dnf install -y redis; svc=redis\n"
            "else yum install -y redis; svc=redis; fi\n"
            "systemctl enable --now \"$svc\""
        ),
    ),

    # ---- polish ----
    dict(
        key="b-motd", name="MOTD / Login Banner", category="OS Setup", icon="terminal",
        section="Configure", description="custom SSH login banner (hides the distro's default noise)",
        input_schema=[
            {"name": "banner", "type": "code",
             "default": "==========================================\n   deployed by GoblinDock\n==========================================\n",
             "label": "Banner text"},
            {"name": "disable_default", "type": "bool", "default": True, "label": "Hide distro MOTD noise"},
        ],
        ansible=(
            "- name: Write MOTD banner\n"
            "  ansible.builtin.copy:\n"
            "    dest: /etc/motd\n"
            "    mode: \"0644\"\n"
            "    content: |\n"
            "      {banner}\n"
            "- name: Hide distro MOTD noise\n"
            "  ansible.builtin.shell: chmod -x /etc/update-motd.d/10-help-text /etc/update-motd.d/50-motd-news /etc/update-motd.d/91-contract-ua-esm-status 2>/dev/null || true\n"
            "  when: {disable_default}"
        ),
        cloudinit=(
            "cat > /etc/motd <<'GDEOF'\n{banner}\nGDEOF\n"
            "if {disable_default}; then chmod -x /etc/update-motd.d/10-help-text /etc/update-motd.d/50-motd-news 2>/dev/null || true; fi"
        ),
    ),
]


# cloud-init = first-boot blocks (identity, or things that must exist BEFORE the
# post-boot ansible leg — swap, so small VMs survive heavy installs); everything
# else is post-boot ansible.
_CLOUDINIT_BLOCKS = {"b-os", "b-clean", "b-conpw", "b-swap"}


def seed_blocks() -> None:
    with session_scope() as s:
        existing = {b.key: b for b in s.exec(select(Block)).all()}
        # Prune built-in blocks that were removed from the catalog (e.g. b-ssh, merged
        # into b-user). `.builtin` is the single source of truth for "one of ours" —
        # the same flag the re-sync below and visibility checks use, so prune and
        # re-sync can never disagree. A user's custom/forked block (builtin=False) is
        # never touched. (`kind` is a descriptive label only; not behaviorally load-bearing.)
        _builtin_keys = {spec["key"] for spec in BUILTIN_BLOCKS}
        for _k, _b in list(existing.items()):
            if _k not in _builtin_keys and _b.builtin:
                s.delete(_b)
        for spec in BUILTIN_BLOCKS:
            phase = "cloudinit" if spec["key"] in _CLOUDINIT_BLOCKS else "ansible"
            cur = existing.get(spec["key"])
            if cur is not None:
                # Re-sync GoblinDock's own built-in blocks with the code on every boot so
                # fixes to their templates (e.g. the ansible quoting hardening) reach
                # existing installs. Never touch a user's custom/forked block.
                if cur.builtin:
                    cur.name = spec["name"]
                    cur.category = spec["category"]
                    cur.icon = spec["icon"]
                    cur.section = spec.get("section", "Install")
                    cur.phase = phase
                    cur.description = spec["description"]
                    cur.input_schema_json = json.dumps(spec.get("input_schema", []))
                    cur.ansible_template = spec.get("ansible", "")
                    cur.cloudinit_template = spec.get("cloudinit", "")
                    s.add(cur)
                continue
            s.add(Block(
                key=spec["key"], name=spec["name"], category=spec["category"],
                icon=spec["icon"], section=spec.get("section", "Install"), phase=phase,
                description=spec["description"],
                input_schema_json=json.dumps(spec.get("input_schema", [])),
                ansible_template=spec.get("ansible", ""),
                cloudinit_template=spec.get("cloudinit", ""),
                builtin=True, kind="builtin",
            ))


# Curated, vetted base cloud images. A maintained picker means users SELECT a known-good
# base instead of pasting an arbitrary URL — which also shrinks the SSRF surface (non-admins
# can only build from a base image, never a raw URL). URLs point at each distro's canonical
# "current/latest" cloud image so they keep resolving as upstream rolls fresh builds; the
# checksum is intentionally left blank for these ROLLING URLs (a pinned digest would mismatch
# and FAIL the build the moment upstream rotates the image). The node-side checksum
# verification still applies whenever an admin pins a digest on a specific entry.
CURATED_BASE_IMAGES = [
    {"name": "Ubuntu 24.04 LTS", "os_family": "ubuntu",
     "source_url": UBUNTU_2404_URL, "size": "~600 MB"},
    {"name": "Ubuntu 22.04 LTS", "os_family": "ubuntu",
     "source_url": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
     "size": "~640 MB"},
    {"name": "Debian 12 (Bookworm)", "os_family": "debian",
     "source_url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
     "size": "~350 MB"},
    {"name": "Rocky Linux 9", "os_family": "rocky",
     "source_url": "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
     "size": "~1.1 GB"},
]


def seed_base_image() -> None:
    """Seed the curated catalog of base cloud images. Idempotent per name, so it adds
    any newly-curated entries on upgrade without touching ones the operator already has."""
    with session_scope() as s:
        for entry in CURATED_BASE_IMAGES:
            if s.exec(select(Image).where(Image.name == entry["name"])).first():
                continue
            s.add(Image(
                kind="base", name=entry["name"], os_family=entry["os_family"],
                source_url=entry["source_url"], checksum=entry.get("checksum", ""),
                build_status="ready", size=entry.get("size", "cloud image"),
            ))


def maybe_seed_admin() -> None:
    with session_scope() as s:
        if s.exec(select(User)).first():
            return
        if settings.admin_email and settings.admin_password:
            s.add(User(
                email=settings.admin_email, name=settings.admin_name,
                password_hash=hash_password(settings.admin_password), role="admin",
            ))


def maybe_seed_proxmox() -> None:
    if not (settings.seed_proxmox and settings.proxmox_token_id and settings.proxmox_token):
        return
    with session_scope() as s:
        if s.exec(select(Connection).where(Connection.name == settings.proxmox_node)).first():
            return
        s.add(Connection(
            name=settings.proxmox_node,
            host=settings.proxmox_host, port=8006,
            token_id=settings.proxmox_token_id,
            token_secret_enc=encrypt(settings.proxmox_token),
            verify_tls=False,
            node=settings.proxmox_node,
            storage=settings.proxmox_storage,
            iso_storage=settings.proxmox_iso_storage,
            snippet_storage=settings.proxmox_snippet_storage,
            bridge=settings.proxmox_bridge,
            ssh_host=settings.proxmox_ssh_host,
            ssh_user=settings.proxmox_ssh_user,
            ssh_key_path=settings.proxmox_ssh_key,
        ))


def seed_templates() -> None:
    """A public starter template so AI dev boxes are one click away. The hostname
    block is ask-on-deploy — the demo shows off deploy-time prompts out of the box.
    Wired to the seeded Ubuntu base image; deployable the moment a Proxmox connection
    exists (the dev seed's connection is linked when present)."""
    with session_scope() as s:
        if s.exec(select(Template).where(Template.name == "AI Dev Box")).first():
            return
        base = s.exec(select(Image).where(
            Image.kind == "base", Image.os_family == "ubuntu")).first()
        conn = s.exec(select(Connection)).first()
        recipe = [
            {"id": "s-os", "name": "OS Setup", "blocks": [
                {"ref": "b-hostname", "name": "Set Hostname",
                 "inputs": {"hostname": ""}, "ask": ["hostname"]},
            ]},
            {"id": "s-inst", "name": "Install", "blocks": [
                {"ref": "b-nodejs", "name": "Node.js (LTS)", "inputs": {"version": "22"}},
                {"ref": "b-claudecode", "name": "Claude Code", "inputs": {"node_version": "22"}},
                {"ref": "b-codex", "name": "OpenAI Codex", "inputs": {"node_version": "22"}},
            ]},
            {"id": "s-conf", "name": "Configure", "blocks": [
                {"ref": "b-claudemd", "name": "Global CLAUDE.md", "inputs": {
                    "user": "goblin",
                    "content": "# Global Claude Code instructions (~/.claude/CLAUDE.md)\n\nBefore answering, reason step by step and verify the answer against all constraints in the request.\n",
                }},
            ]},
        ]
        s.add(Template(
            name="AI Dev Box",
            description="Node.js + Claude Code + OpenAI Codex + a global CLAUDE.md — a ready-to-code box.",
            os_family="ubuntu", recipe_json=json.dumps(recipe),
            base_image_id=base.id if base else None,
            connection_id=conn.id if conn else None,
            default_cpu=1, default_ram=2, default_disk=20, public=True, owner_id=None,
        ))


def seed_default_networks() -> None:
    """Ensure every connection has at least a default DHCP network. Done at startup (a
    write) so GET /api/state never has to create one lazily during a read. New
    connections get theirs in the add-connection handler."""
    with session_scope() as s:
        for c in s.exec(select(Connection)).all():
            if not s.exec(select(Network).where(Network.connection_id == c.id)).first():
                s.add(Network(connection_id=c.id, name="lan-dhcp", mode="dhcp",
                              bridge=c.bridge or "vmbr0", created_by=c.created_by))


def run_all_seeds() -> None:
    seed_blocks()
    seed_base_image()
    maybe_seed_admin()
    maybe_seed_proxmox()
    seed_templates()          # after seed_base_image + maybe_seed_proxmox so the template can wire both
    seed_default_networks()   # after maybe_seed_proxmox so the seeded connection gets one

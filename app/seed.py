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
        key="b-apt", name="Install Packages", category="Packages", icon="package",
        section="Install", description="apt package list",
        input_schema=[
            {"name": "packages", "type": "tags", "default": ["build-essential", "git", "curl", "htop", "jq"], "label": "Packages"},
        ],
        ansible="- name: Install Packages\n  ansible.builtin.apt:\n    name: {packages_yaml}\n    state: present\n    update_cache: true",
        cloudinit="export DEBIAN_FRONTEND=noninteractive\napt-get update -y\napt-get install -y {packages}",
    ),
    dict(
        key="b-ssh", name="User & SSH Key", category="Users / SSH", icon="key",
        section="Configure", description="create user, push key, sudo",
        input_schema=[
            {"name": "user", "type": "text", "default": "goblin", "label": "Username"},
            {"name": "public_key", "type": "secret", "default": "{{ secrets.TEAM_SSH_PUBKEY }}", "label": "Public key"},
            {"name": "sudo", "type": "bool", "default": True, "label": "Passwordless sudo"},
        ],
        ansible="- name: User & SSH Key\n  ansible.builtin.user:\n    name: {user}\n    groups: [sudo]\n    shell: /bin/bash",
        cloudinit="id {user} >/dev/null 2>&1 || useradd -m -s /bin/bash {user}\nusermod -aG sudo {user} || true\ninstall -d -m700 /home/{user}/.ssh\nprintf '%s\\n' {public_key} >> /home/{user}/.ssh/authorized_keys\nchown -R {user}:{user} /home/{user}/.ssh\nchmod 600 /home/{user}/.ssh/authorized_keys",
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
        section="Cleanup", description="apt clean + fstrim",
        input_schema=[],
        ansible="- name: Cleanup & Trim\n  ansible.builtin.shell: apt-get clean && fstrim -av || true",
        cloudinit="apt-get clean || true\nfstrim -av || true",
    ),

    # ---- extended pre-built blocks (Ansible-module backed, simple inputs) ----
    dict(
        key="b-user", name="Create User", category="Users / SSH", icon="key",
        section="Configure", description="create a Linux user + groups",
        input_schema=[
            {"name": "user", "type": "text", "default": "deploy", "label": "Username"},
            {"name": "groups", "type": "tags", "default": ["sudo"], "label": "Groups"},
            {"name": "shell", "type": "text", "default": "/bin/bash", "label": "Login shell"},
        ],
        ansible=(
            "- name: Create User\n"
            "  ansible.builtin.user:\n"
            "    name: {user}\n"
            "    groups: {groups_yamlq}\n"
            "    append: true\n"
            "    create_home: true\n"
            "    shell: {shell}"
        ),
        cloudinit="id {user} >/dev/null 2>&1 || useradd -m -s {shell} {user}",
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
        key="b-ufw", name="Firewall Rule (UFW)", category="Services", icon="shield",
        section="Configure", description="allow/deny a port (community.general)",
        input_schema=[
            {"name": "port", "type": "text", "default": "22", "label": "Port"},
            {"name": "proto", "type": "select", "options": ["tcp", "udp"], "default": "tcp", "label": "Protocol"},
            {"name": "rule", "type": "select", "options": ["allow", "deny", "limit", "reject"], "default": "allow", "label": "Rule"},
        ],
        ansible=(
            "- name: Firewall Rule (UFW)\n"
            "  community.general.ufw:\n"
            "    rule: {rule}\n"
            "    port: \"{port}\"\n"
            "    proto: {proto}"
        ),
        cloudinit="ufw {rule} {port}/{proto} || true",
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
            "  ansible.builtin.apt:\n"
            "    name: python3-pip\n"
            "    state: present\n"
            "    update_cache: true\n"
            "- name: Pip Packages\n"
            "  ansible.builtin.pip:\n"
            "    name: {packages_yamlq}\n"
            "    state: present"
        ),
        cloudinit="apt-get install -y python3-pip\npip3 install {packages}",
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
        key="b-pgdb", name="PostgreSQL DB + User", category="Services", icon="sliders",
        section="Install", description="create a database + user (community.postgresql)",
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
        ansible=(
            "- name: Install Node.js\n"
            "  ansible.builtin.shell: |\n"
            "    curl -fsSL https://deb.nodesource.com/setup_{version_q}.x | bash -\n"
            "    apt-get install -y nodejs"
        ),
        cloudinit="curl -fsSL https://deb.nodesource.com/setup_{version}.x | bash -\napt-get install -y nodejs",
    ),
    dict(
        key="b-claudecode", name="Claude Code", category="AI Tools", icon="spark",
        section="Install", description="Anthropic Claude Code CLI (npm global)",
        input_schema=[
            {"name": "node_version", "type": "text", "default": "22", "label": "Node version (installed if absent)"},
        ],
        ansible=(
            "- name: Install Claude Code\n"
            "  ansible.builtin.shell: |\n"
            "    command -v npm >/dev/null 2>&1 || { curl -fsSL https://deb.nodesource.com/setup_{node_version_q}.x | bash - && apt-get install -y nodejs; }\n"
            "    npm install -g @anthropic-ai/claude-code"
        ),
        cloudinit="command -v npm >/dev/null 2>&1 || { curl -fsSL https://deb.nodesource.com/setup_{node_version}.x | bash - && apt-get install -y nodejs; }\nnpm install -g @anthropic-ai/claude-code",
    ),
    dict(
        key="b-codex", name="OpenAI Codex", category="AI Tools", icon="spark",
        section="Install", description="OpenAI Codex CLI (npm global)",
        input_schema=[
            {"name": "node_version", "type": "text", "default": "22", "label": "Node version (installed if absent)"},
        ],
        ansible=(
            "- name: Install OpenAI Codex\n"
            "  ansible.builtin.shell: |\n"
            "    command -v npm >/dev/null 2>&1 || { curl -fsSL https://deb.nodesource.com/setup_{node_version_q}.x | bash - && apt-get install -y nodejs; }\n"
            "    npm install -g @openai/codex"
        ),
        cloudinit="command -v npm >/dev/null 2>&1 || { curl -fsSL https://deb.nodesource.com/setup_{node_version}.x | bash - && apt-get install -y nodejs; }\nnpm install -g @openai/codex",
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
        section="Configure", description="set a password so a user can log in at the console (serial/VNC)",
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
]


# cloud-init = first-boot identity (must run at boot); everything else is post-boot ansible.
_CLOUDINIT_BLOCKS = {"b-os", "b-ssh", "b-clean", "b-conpw"}


def seed_blocks() -> None:
    with session_scope() as s:
        existing = {b.key: b for b in s.exec(select(Block)).all()}
        for spec in BUILTIN_BLOCKS:
            phase = spec.get("phase") or ("cloudinit" if spec["key"] in _CLOUDINIT_BLOCKS else "ansible")
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
                builtin=spec.get("builtin", True),
                kind="builtin" if spec.get("builtin", True) else "custom",
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
    Base-image/connection wiring is added by the templates-only seed task — until then
    the template is editable but not deployable."""
    with session_scope() as s:
        if s.exec(select(Template).where(Template.name == "AI Dev Box")).first():
            return
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
    seed_templates()
    seed_base_image()
    maybe_seed_admin()
    maybe_seed_proxmox()
    seed_default_networks()   # after maybe_seed_proxmox so the seeded connection gets one

#!/usr/bin/env python3
"""
End-to-end test of install.sh on one machine, without pulling images.

    python3 tests/installer/run.py            # all scenarios
    python3 tests/installer/run.py s2 s3      # some of them

Needs: a caddy binary (CADDY_BIN, default /tmp/caddy) and the real docker
CLI for `docker compose config` (the daemon is not used). For the owner
flow (step "e2e") a real 3x-ui 3.x panel from scripts/dev-3xui.sh:
XUI_TEST_V3_URL / XUI_TEST_V3_TOKEN / XUI_TEST_V3_INBOUND.

Each scenario runs install.sh as a person would — through a pseudo
terminal, answering the questions it asks — and then checks what it left
on disk, what Telegram was told and that the shop answers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
WORK = Path(os.environ.get("IT_DIR", "/tmp/it"))
FAKE = WORK / "fake"
TOKEN = "7777777777:AAHfakeTokenForInstallerTests_123456"
OWNER = 555666777
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
results: list[tuple[str, bool, str]] = []


# ----------------------------------------------------------------- helpers
def check(name: str, cond: bool, detail: str = "") -> bool:
    results.append((name, bool(cond), detail))
    print(("  ok   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    return bool(cond)


def env_for(scenario: dict, **extra) -> dict:
    FAKE.mkdir(parents=True, exist_ok=True)
    (FAKE / "scenario.json").write_text(json.dumps(scenario))
    e = dict(os.environ)
    e.update({
        "PATH": f"{HERE / 'bin'}:{e['PATH']}",
        "FAKE_DIR": str(FAKE),
        "TG_API_BASE": "http://127.0.0.1:18081",
        "VPN_SHOP_LOG": str(WORK / "install.log"),
        "LANG": "C.UTF-8",
        "NO_CLEAR": "1",
        # the "Update" button's systemd units go here, never into the real /etc/systemd
        "VPN_SELLER_SYSTEMD_DIR": str(WORK / "systemd"),
    })
    e.update(extra)
    return e


def read_env(d: Path) -> dict:
    out = {}
    for line in (d / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def tg_calls() -> list[dict]:
    f = FAKE / "tg.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def reset_tg():
    (FAKE / "tg.jsonl").unlink(missing_ok=True)
    # the fake keeps the webhook in memory: clear it like a fresh bot
    subprocess.run(["curl", "-s", "-o", "/dev/null", "-X", "POST",
                    f"http://127.0.0.1:18081/bot{TOKEN}/deleteWebhook"])


def stop_app():
    p = FAKE / "app.pid"
    if p.exists():
        try:
            os.killpg(int(p.read_text()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
        p.unlink(missing_ok=True)
        time.sleep(0.5)


def run_pty(cmd: str, env: dict, answers: list[tuple[str, str]], timeout: int = 240) -> tuple[int, str]:
    """Run `cmd` in a pseudo terminal. `answers` is a list of
    (regex seen on screen, text to type); each is used once, in order."""
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(str(WORK))
        os.execvpe("bash", ["bash", "-c", cmd], env)
    out, pending, deadline = "", list(answers), time.time() + timeout
    seen_upto = 0
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 4096).decode("utf-8", "replace")
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        clean = ANSI.sub("", out)
        if pending and re.search(pending[0][0], clean[seen_upto:]):
            m = re.search(pending[0][0], clean[seen_upto:])
            seen_upto += m.end()
            time.sleep(0.15)
            os.write(fd, (pending[0][1] + "\r").encode())
            pending.pop(0)
    else:
        os.kill(pid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    text = ANSI.sub("", out).replace("\r", "")
    if pending:
        text += f"\n[runner] unanswered: {pending[0][0]}"
    return code, text


def run_plain(args: list[str], env: dict, stdin: str = "", timeout: int = 240) -> tuple[int, str]:
    r = subprocess.run(args, env=env, input=stdin, capture_output=True, text=True,
                       timeout=timeout, cwd=str(WORK))
    return r.returncode, r.stdout + r.stderr


def fresh_dir(name: str) -> Path:
    stop_app()
    d = WORK / name
    shutil.rmtree(d, ignore_errors=True)
    return d


def save_output(name: str, text: str):
    (WORK / f"{name}.out").write_text(text)


def webhook_now() -> str:
    import urllib.request
    raw = urllib.request.urlopen(f"http://127.0.0.1:18081/bot{TOKEN}/getWebhookInfo").read()
    return json.loads(raw)["result"]["url"]


# ----------------------------------------------------------------- scenarios
def s1_clean_domain_unattended():
    """Clean server, domain, no questions (flags)."""
    print("\n[s1] clean server + domain, unattended")
    d = fresh_dir("s1")
    reset_tg()
    code, out = run_plain(["bash", str(ROOT / "install.sh"), "install", "--domain", "shop.example.com",
                           "--token", TOKEN, "--admin-id", str(OWNER), "--yes"],
                          env_for({}, VPN_SHOP_DIR=str(d)))
    save_output("s1", out)
    check("s1 exit code 0", code == 0, out[-800:])
    e = read_env(d)
    check("s1 mode caddy", e.get("INSTALL_MODE") == "caddy" and e.get("COMPOSE_PROFILES") == "caddy", str(e))
    check("s1 no override file", not (d / "docker-compose.override.yml").exists())
    check("s1 .env is 600", oct((d / ".env").stat().st_mode)[-3:] == "600")
    check("s1 secret 64 hex", re.fullmatch(r"[0-9a-f]{64}", e.get("SECRET_KEY", "")) is not None)
    check("s1 password generated (16)", len(e.get("ADMIN_PASSWORD", "")) == 16)
    check("s1 password shown in summary", e.get("ADMIN_PASSWORD", "x") in out)
    cfg = json.loads((FAKE / "compose.json").read_text())
    check("s1 compose: app + caddy", set(cfg["services"]) == {"app", "caddy"}, str(cfg["services"].keys()))
    check("s1 app not published", not cfg["services"]["app"].get("ports"))
    check("s1 webhook set by the shop", webhook_now() == "https://shop.example.com/api/bot/webhook", webhook_now())
    menus = [c for c in tg_calls() if c["method"] == "setChatMenuButton"]
    check("s1 menu button → /app", bool(menus) and menus[-1]["body"]["menu_button"]["web_app"]["url"]
          == "https://shop.example.com/app")
    check("s1 summary printed", "VPN Seller установлен" in out and "https://shop.example.com/admin" in out)
    check("s1 update button set up", (WORK / "systemd" / "vpn-seller-update.path").exists()
          and (d / "data" / "updater.json").exists())
    return d


def s2_curl_pipe_tunnel_interactive():
    """`curl … | bash` on a clean server with no domain: menu → questions → tunnel."""
    print("\n[s2] curl | bash, interactive, no domain → tunnel, ID detected by message")
    d = fresh_dir("s2")
    reset_tg()
    repo = WORK / "repo.git"
    answers = [
        (r"Ваш выбор", "1"),                               # menu: install
        (r"› Токен", "123:not-a-token"),                   # a typo first
        (r"не похоже на токен", ""),                       # (nothing to type, just seen)
        (r"› Токен", TOKEN),
        (r"Ваш выбор", "1"),                               # detect my ID
        (r"Ваш выбор", "2"),                               # no domain
        (r"› Пароль", "short"),
        (r"от 8 символов", ""),
        (r"› Пароль", "Sandbox-Pass-2026"),
        (r"RUB, USD", ""),                                 # currency: Enter = RUB
        (r"начинаю\?", ""),                                # confirm: Enter = yes
    ]
    # the "just seen" entries type an empty line; drop them (they only
    # anchor the order of what appears on screen)
    answers = [a for a in answers if a[1] != "" or a[0] in (r"RUB, USD", r"начинаю\?")]
    env = env_for({}, VPN_SHOP_DIR=str(d), VPN_SHOP_REPO=f"file://{repo}")
    code, out = run_pty(f"cat {ROOT / 'install.sh'} | bash", env, answers)
    save_output("s2", out)
    check("s2 exit code 0", code == 0, out[-1500:])
    check("s2 banner + menu shown", "S H O P" in out and "Установить" in out)
    check("s2 bad token rejected", "не похоже на токен" in out)
    check("s2 short password rejected", "от 10 символов" in out)
    check("s2 code came from git", (d / ".git").is_dir())
    e = read_env(d)
    check("s2 admin ID detected from the message", e.get("ADMIN_TELEGRAM_IDS") == str(OWNER), e.get("ADMIN_TELEGRAM_IDS"))
    check("s2 mode tunnel", e.get("INSTALL_MODE") == "tunnel" and e.get("COMPOSE_PROFILES") == "tunnel")
    check("s2 password kept", e.get("ADMIN_PASSWORD") == "Sandbox-Pass-2026")
    check("s2 PUBLIC_URL left empty (follows tunnel)", e.get("PUBLIC_URL") == "" and e.get("TUNNEL_METRICS_URL") == "http://tunnel:2000")
    cfg = json.loads((FAKE / "compose.json").read_text())
    check("s2 compose: app + tunnel", set(cfg["services"]) == {"app", "tunnel"})
    want = "https://sandbox-test-tunnel.trycloudflare.com/api/bot/webhook"
    check("s2 webhook follows the tunnel", webhook_now() == want, webhook_now())
    check("s2 bot linked message", "бот подключён" in out)
    return d


CADDY_ORIG = """{
\temail ops@example.org
}

:80 {
\trespond "omniroute placeholder"
}
"""


def s3_existing_caddy_interactive():
    """Ports held by another project's Caddy in Docker: plug into it."""
    print("\n[s3] another project's Caddy on 80/443 (interactive, run as a file)")
    d = fresh_dir("s3")
    reset_tg()
    omni = WORK / "omni"
    shutil.rmtree(omni, ignore_errors=True)
    omni.mkdir(parents=True)
    cf = omni / "Caddyfile"
    cf.write_text(CADDY_ORIG)
    inode = cf.stat().st_ino
    scen = {"proxy": {"name": "caddy", "image": "caddy:2-alpine", "wd": str(omni), "net": "omniroute_default",
                      "cfg_host": str(cf), "cfg_ctr": "/etc/caddy/Caddyfile"}}
    answers = [
        (r"Ваш выбор", "1"),
        (r"› Токен", TOKEN),
        (r"Ваш выбор", "2"),                    # type the ID
        (r"Telegram ID \(цифры", str(OWNER)),
        (r"Ваш выбор", "1"),                    # I have a domain
        (r"Домен, например", "Shop3.Example.com"),
        (r"› Пароль", ""),                      # generate
        (r"RUB, USD", "usd"),
        (r"начинаю\?", "д"),
    ]
    code, out = run_pty(f"bash {ROOT / 'install.sh'}", env_for(scen, VPN_SHOP_DIR=str(d)), answers)
    save_output("s3", out)
    check("s3 exit code 0", code == 0, out[-1500:])
    check("s3 found the other Caddy", "Caddy другого проекта" in out)
    e = read_env(d)
    check("s3 mode external", e.get("INSTALL_MODE") == "external" and e.get("COMPOSE_PROFILES") == "")
    check("s3 domain lower-cased", e.get("DOMAIN") == "shop3.example.com")
    check("s3 currency upper-cased", e.get("CURRENCY") == "USD")
    text = cf.read_text()
    check("s3 site block added", "shop3.example.com {" in text and "reverse_proxy vpnshop-app:8000" in text, text)
    check("s3 other sites untouched", text.startswith(CADDY_ORIG))
    check("s3 Caddyfile inode kept (bind mount)", cf.stat().st_ino == inode)
    check("s3 validated with real caddy", "caddy validate -> 0" in (FAKE / "docker.log").read_text())
    check("s3 Caddy reloaded", (FAKE / "caddy.reloaded").exists())
    ov = (d / "docker-compose.override.yml").read_text()
    check("s3 override joins omniroute_default", "name: omniroute_default" in ov and "vpnshop-app" in ov)
    cfg = json.loads((FAKE / "compose.json").read_text())
    app = cfg["services"]["app"]
    check("s3 compose: only app, nothing published", set(cfg["services"]) == {"app"} and not app.get("ports"))
    check("s3 app on both networks", set(app["networks"]) == {"default", "proxy"}, str(app.get("networks")))
    check("s3 webhook", webhook_now() == "https://shop3.example.com/api/bot/webhook", webhook_now())
    return d, cf


def s3b_broken_caddy_rolls_back():
    """The other Caddyfile already serves the same domain: validate fails → restore."""
    print("\n[s3b] caddy validate fails → Caddyfile restored, manual instructions")
    d = fresh_dir("s3b")
    reset_tg()
    omni = WORK / "omni-b"
    shutil.rmtree(omni, ignore_errors=True)
    omni.mkdir(parents=True)
    cf = omni / "Caddyfile"
    orig = CADDY_ORIG + "\nshop4.example.com {\n\trespond \"someone else\"\n}\n"
    cf.write_text(orig)
    scen = {"proxy": {"name": "caddy", "image": "caddy:2", "wd": str(omni), "net": "omniroute_default",
                      "cfg_host": str(cf)}}
    code, out = run_plain(["bash", str(ROOT / "install.sh"), "install", "--domain", "shop4.example.com",
                           "--token", TOKEN, "--admin-id", str(OWNER), "--yes"],
                          env_for(scen, VPN_SHOP_DIR=str(d)))
    save_output("s3b", out)
    check("s3b exit code 0", code == 0, out[-800:])
    check("s3b validate error reported", "вернул Caddyfile как было" in out, out[-1200:])
    check("s3b Caddyfile byte-identical", cf.read_text() == orig)
    check("s3b manual instructions shown", "reverse_proxy vpnshop-app:8000" in out)


def s4_host_nginx():
    print("\n[s4] nginx on the host holds 80/443, unattended")
    d = fresh_dir("s4")
    reset_tg()
    code, out = run_plain(["bash", str(ROOT / "install.sh"), "install", "--domain", "shop5.example.com",
                           "--token", TOKEN, "--admin-id", str(OWNER), "--password", "Nginx-Case-01", "--yes"],
                          env_for({"host_proxy": "nginx"}, VPN_SHOP_DIR=str(d)))
    save_output("s4", out)
    check("s4 exit code 0", code == 0, out[-800:])
    e = read_env(d)
    check("s4 mode local", e.get("INSTALL_MODE") == "local")
    cfg = json.loads((FAKE / "compose.json").read_text())
    ports = cfg["services"]["app"].get("ports") or []
    check("s4 published on 127.0.0.1:8080 only",
          len(ports) == 1 and ports[0].get("host_ip") == "127.0.0.1" and str(ports[0].get("published")) == "8080",
          str(ports))
    check("s4 nginx snippet shown", "proxy_pass http://127.0.0.1:8080;" in out)


def s5_update_and_status(d: Path):
    print("\n[s5] update + status on the s1 install")
    stop_app()
    env = env_for({}, VPN_SHOP_DIR=str(d))
    # s1 was copied from a folder without git: update rebuilds only
    code, out = run_plain(["bash", str(d / "install.sh"), "update"], env)
    save_output("s5-update", out)
    check("s5 update exit 0", code == 0, out[-800:])
    check("s5 update made a backup", any((d / "data" / "backups").glob("*.zip")) if (d / "data" / "backups").exists() else "Резервная копия" in out)
    code, out = run_plain(["bash", str(d / "install.sh"), "status"], env)
    save_output("s5-status", out)
    check("s5 status exit 0", code == 0, out[-800:])
    check("s5 status: shop answers", "магазин отвечает" in out)
    check("s5 status: bot linked", "бот подключён" in out)


def make_tarball(name: str, version: str, broken: bool = False) -> Path:
    """The repository archive the way GitHub's API serves it: one top folder."""
    import tarfile
    stage = WORK / "stage" / name
    shutil.rmtree(stage.parent, ignore_errors=True)
    shutil.copytree(WORK / "src", stage, ignore=shutil.ignore_patterns(".git"))
    (stage / "VERSION").write_text(version + "\n")
    if broken:          # imports, but the app cannot start
        with open(stage / "app" / "main.py", "a") as f:
            f.write("\nraise RuntimeError('broken build for the rollback test')\n")
    out = WORK / "gh" / "repos" / "test" / "vpn-seller" / "tarball" / "main"
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        tar.add(stage, arcname="test-vpn-seller-0a1b2c3")
    return out


def health_version() -> str:
    try:
        r = subprocess.run(["/usr/bin/curl", "-fsS", "--max-time", "5", "http://127.0.0.1:18000/health"],
                           capture_output=True, text=True)
        return json.loads(r.stdout).get("version", "")
    except (ValueError, OSError):
        return ""


def s9_update_from_github(d: Path):
    print("\n[s9] update from GitHub (archive), the panel's request, rollback, private repo")
    stop_app()
    envf = d / ".env"
    envf.write_text(envf.read_text() + "UPDATE_REPO=https://github.com/test/vpn-seller\n")
    env = env_for({}, VPN_SHOP_DIR=str(d), VPN_SHOP_GITHUB_API=f"file://{WORK / 'gh'}", VPN_SHOP_WAIT_TRIES="10")
    before = (d / "VERSION").read_text().strip()
    db_before = (d / "data" / "vpn.db").stat().st_size if (d / "data" / "vpn.db").exists() else -1
    # a good new version, asked from the panel
    make_tarball("good", "9.9.0")
    (d / "data" / "update.request").write_text(json.dumps({"at": int(time.time()), "by": "admin", "to": "9.9.0"}))
    code, out = run_plain(["bash", str(d / "install.sh"), "update", "--from-panel"], env)
    save_output("s9-update", out)
    check("s9 update exit 0", code == 0, out[-1200:])
    check("s9 new code in place", (d / "VERSION").read_text().strip() == "9.9.0")
    check("s9 the shop answers with the new version", health_version() == "9.9.0", health_version())
    check("s9 request taken", not (d / "data" / "update.request").exists())
    st = json.loads((d / "data" / "update.status").read_text())
    check("s9 status ok with versions", st["state"] == "ok" and st["from"] == before and st["to"] == "9.9.0", str(st)[:300])
    check("s9 .env and the base kept", "BOT_TOKEN=" + TOKEN in envf.read_text()
          and (d / "data" / "vpn.db").exists() and db_before != -1)
    unit = (WORK / "systemd" / "vpn-seller-update.service").read_text()
    path = (WORK / "systemd" / "vpn-seller-update.path").read_text()
    check("s9 updater units", f"ExecStart=/bin/bash {d}/install.sh update --from-panel" in unit
          and f"PathExists={d}/data/update.request" in path, unit + path)
    check("s9 previous code archive removed", not (d / "data" / "update-prev.tgz").exists())
    # a version that does not start: the previous one comes back by itself
    make_tarball("broken", "9.9.1", broken=True)
    code, out = run_plain(["bash", str(d / "install.sh"), "update"], env)
    save_output("s9-rollback", out)
    check("s9 broken update fails", code != 0)
    check("s9 rolled back", (d / "VERSION").read_text().strip() == "9.9.0"
          and "broken build" not in (d / "app" / "main.py").read_text(), out[-800:])
    check("s9 the shop answers after the rollback", health_version() == "9.9.0", health_version())
    check("s9 rollback said so", "9.9.0" in out and "вернул" in out, out[-600:])
    st = json.loads((d / "data" / "update.status").read_text())
    check("s9 status failed with a reason", st["state"] == "failed" and st["from"] == "9.9.0"
          and st["to"] == "9.9.1" and "не отвечает" in st["reason"] and st["log"], str(st)[:300])
    # GitHub refuses (a private repository without a token): nothing changes
    env2 = dict(env, VPN_SHOP_GITHUB_API=f"file://{WORK / 'nowhere'}")
    code, out = run_plain(["bash", str(d / "install.sh"), "update"], env2)
    save_output("s9-private", out)
    st = json.loads((d / "data" / "update.status").read_text())
    check("s9 refused download fails cleanly", code != 0 and "GITHUB_TOKEN" in out and st["state"] == "failed"
          and (d / "VERSION").read_text().strip() == "9.9.0", out[-600:])
    check("s9 the shop still answers", health_version() == "9.9.0", health_version())


def s6_uninstall_restores_caddyfile(d: Path, cf: Path):
    print("\n[s6] uninstall the s3 install → other Caddyfile as it was")
    code, out = run_plain(["bash", str(d / "install.sh"), "uninstall"],
                          env_for({"proxy": {"name": "caddy", "image": "caddy:2", "wd": str(cf.parent),
                                             "net": "omniroute_default", "cfg_host": str(cf)}},
                                  VPN_SHOP_DIR=str(d)),
                          stdin="д\nн\n")
    save_output("s6", out)
    check("s6 exit 0", code == 0, out[-800:])
    check("s6 Caddyfile back to the original", cf.read_text() == CADDY_ORIG, repr(cf.read_text()[-200:]))
    check("s6 data kept when asked", (d / "data").is_dir())


def s7_menu_switch_tunnel_to_domain(d: Path):
    print("\n[s7] installed with a tunnel; later: menu → change address → domain")
    reset_tg()
    answers = [
        (r"Ваш выбор", "6"),
        (r"Ваш выбор", "1"),
        (r"Домен, например", "shop7.example.com"),
        (r"Применить\?", ""),
        (r"вернуться в меню", ""),
        (r"Ваш выбор", "0"),
    ]
    code, out = run_pty(f"bash {d / 'install.sh'}", env_for({}, VPN_SHOP_DIR=str(d)), answers)
    save_output("s7", out)
    check("s7 exit 0", code == 0, out[-1500:])
    check("s7 menu shows running", "работает" in out)
    e = read_env(d)
    check("s7 mode caddy now", e.get("INSTALL_MODE") == "caddy" and e.get("DOMAIN") == "shop7.example.com")
    check("s7 tunnel off", e.get("TUNNEL_METRICS_URL") == "" and e.get("COMPOSE_PROFILES") == "caddy")
    check("s7 webhook moved to the domain", webhook_now() == "https://shop7.example.com/api/bot/webhook", webhook_now())


def s8_payments_from_menu(d: Path):
    print("\n[s8] menu → payment methods → YooKassa keys")
    answers = [
        (r"Ваш выбор", "5"),
        (r"Ваш выбор", "2"),
        (r"› shopId", "506751"),
        (r"› Секретный ключ", "live_SandboxSecretKey0123"),
        (r"вернуться в меню", ""),
        (r"Ваш выбор", "0"),
    ]
    code, out = run_pty(f"bash {d / 'install.sh'}", env_for({}, VPN_SHOP_DIR=str(d)), answers)
    save_output("s8", out)
    check("s8 exit 0", code == 0, out[-1500:])
    e = read_env(d)
    check("s8 keys written", e.get("CARD_GATEWAY") == "yookassa" and e.get("YOOKASSA_SHOP_ID") == "506751"
          and e.get("YOOKASSA_SECRET_KEY") == "live_SandboxSecretKey0123")
    check("s8 secret not echoed", "live_SandboxSecretKey0123" not in out)
    check("s8 notification URL shown", "https://shop.example.com/api/pay/yookassa" in out)
    import httpx
    cfg = httpx.get("http://127.0.0.1:18000/api/app/config", trust_env=False).json()
    check("s8 the shop now offers cards", any(m.get("id") == "card" for m in cfg.get("methods") or []), str(cfg.get("methods")))

    print("\n[s8b] menu → payment methods → «Мой налог»")
    answers = [
        (r"Ваш выбор", "5"),
        (r"Ваш выбор", "11"),
        (r"› ИНН", "12345"),                  # too short: asked again
        (r"› ИНН", "500100732259"),
        (r"› Пароль от «Мой налог»", "Nalog-pass-77"),
        (r"вернуться в меню", ""),
        (r"Ваш выбор", "0"),
    ]
    code, out = run_pty(f"bash {d / 'install.sh'}", env_for({}, VPN_SHOP_DIR=str(d)), answers)
    save_output("s8b", out)
    check("s8b exit 0", code == 0, out[-1500:])
    e = read_env(d)
    check("s8b INN and password written", e.get("NALOG_INN") == "500100732259"
          and e.get("NALOG_PASSWORD") == "Nalog-pass-77")
    check("s8b short INN refused", "ровно 12 цифр" in out)
    check("s8b password not echoed", "Nalog-pass-77" not in out)


# ----------------------------------------------------------------- owner flow
def init_data(uid: int) -> str:
    user = json.dumps({"id": uid, "first_name": "Buyer", "language_code": "ru"}, separators=(",", ":"))
    fields = {"auth_date": str(int(time.time())), "query_id": "AAE", "user": user}
    check_str = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def e2e_owner_flow(d: Path):
    """After the install: the owner adds a real 3x-ui panel in the admin
    panel, a customer takes the trial and then pays in Stars, and the keys
    really exist on the panel."""
    import httpx
    print("\n[e2e] owner adds a real 3x-ui panel → customer trial → Stars purchase → keys")
    url = os.environ.get("XUI_TEST_V3_URL")
    if not url:
        check("e2e real 3x-ui panel available (scripts/dev-3xui.sh)", False, "XUI_TEST_V3_URL not set")
        return
    e = read_env(d)
    c = httpx.Client(base_url="http://127.0.0.1:18000", timeout=30, trust_env=False)
    r = c.post("/api/admin/login", json={"login": "admin", "password": e["ADMIN_PASSWORD"]})
    check("e2e admin login with the installer's password", r.status_code == 200, r.text)
    H = {"Authorization": "Bearer " + r.json()["token"]}
    body = {"name": "Germany", "country": "DE", "url": url, "api_token": os.environ["XUI_TEST_V3_TOKEN"],
            "inbound_ids": os.environ.get("XUI_TEST_V3_INBOUND", "1"), "kind": "3xui"}
    r = c.post("/api/admin/servers/test", json=body, headers=H)
    check("e2e «Проверить» connects to the panel", r.status_code == 200 and r.json().get("ok"), r.text)
    r = c.post("/api/admin/servers", json=body, headers=H)
    check("e2e server saved", r.status_code == 200, r.text)
    sid = r.json()["id"]
    r = c.post(f"/api/admin/servers/{sid}/check", headers=H)
    check("e2e server check", r.status_code == 200, r.text)

    buyer = 424242000 + int(time.time()) % 1000
    U = {"X-Init-Data": init_data(buyer)}
    # A fresh shop sells nothing: the owner adds a plan from the template
    # and turns the trial on, as the launch checklist says.
    cfg = c.get("/api/app/config", headers=U).json()
    check("e2e fresh shop has no plans and no trial", cfg.get("plans") == [], str(cfg.get("plans"))[:200])
    steps = {s["id"]: s["done"] for s in c.get("/api/admin/setup", headers=H).json()["steps"]}
    check("e2e launch checklist: server done, plans not yet", steps.get("server") and not steps.get("plans"), str(steps))
    tpl = c.get("/api/admin/templates", headers=H).json()["plans"][0]
    r = c.post("/api/admin/plans", headers=H, json={"name": tpl["name"], "name_en": tpl["name_en"],
                                                    "days": tpl["days"], "price": tpl["price"], "devices": 3})
    check("e2e plan added from the template", r.status_code == 200, r.text)
    r = c.put("/api/admin/trial", headers=H, json={"enabled": True, "days": 3, "devices": 1})
    check("e2e trial turned on", r.status_code == 200 and r.json().get("enabled") is True, r.text)
    cfg = c.get("/api/app/config", headers=U).json()
    plans = cfg.get("plans") or []
    trial = next((p for p in plans if p.get("is_trial")), None)
    paid = next((p for p in plans if not p.get("is_trial")), None)
    check("e2e plans exist now (trial + paid)", bool(trial and paid), str(plans)[:300])
    me = c.get("/api/app/me", headers=U).json()
    check("e2e new customer may take the trial", me.get("trial_available") is True)
    r = c.post("/api/app/order", json={"plan_id": trial["id"], "kind": "purchase"}, headers=U)
    check("e2e trial activated", r.status_code == 200 and r.json().get("status") == "success", r.text)
    me = c.get("/api/app/me", headers=U).json()
    sub = me.get("sub_url", "")
    check("e2e subscription link issued", "/sub/" in sub, sub)
    r = c.get("/sub/" + sub.rsplit("/sub/", 1)[1], headers={"User-Agent": "Happ/3.1"})
    links = base64.b64decode(r.text).decode().splitlines() if r.status_code == 200 else []
    check("e2e subscription returns vless keys", any(x.startswith("vless://") for x in links), r.text[:200])
    exp_trial = me["expires_at"]

    # The client really exists on the panel.
    sys.path.insert(0, str(ROOT))
    from app.providers import XuiPanel
    from types import SimpleNamespace
    panel = XuiPanel(SimpleNamespace(id=99991, url=url, username="", password="",
                                     api_token=os.environ["XUI_TEST_V3_TOKEN"],
                                     inbound_ids=os.environ.get("XUI_TEST_V3_INBOUND", "1"),
                                     sub_url="", verify_tls=True, api_version=""))
    got = panel._request("GET", f"panel/api/clients/get/tg{buyer}")
    check("e2e client tg<id> exists on the 3x-ui panel", bool(got.get("success")), str(got)[:200])

    # A paid plan in Stars: invoice, then Telegram's two updates.
    r = c.post("/api/app/order", json={"plan_id": paid["id"], "kind": "purchase", "method": "stars"}, headers=U)
    ok = r.status_code == 200 and (r.json().get("pay") or {}).get("type") == "invoice"
    check("e2e Stars invoice created", ok, r.text)
    order_id = r.json()["order_id"]
    stars = r.json()["pay"]["stars"]
    secret = hashlib.sha256(("tg-webhook:" + e["SECRET_KEY"]).encode()).hexdigest()[:48]
    W = {"X-Telegram-Bot-Api-Secret-Token": secret}
    pre = {"update_id": 900, "pre_checkout_query": {"id": "pq1", "from": {"id": buyer}, "currency": "XTR",
                                                   "total_amount": stars, "invoice_payload": f"tx:{order_id}"}}
    r = c.post("/api/bot/webhook", json=pre, headers=W)
    answer = [x for x in tg_calls() if x["method"] == "answerPreCheckoutQuery"]
    check("e2e pre-checkout accepted", r.status_code == 200 and answer and answer[-1]["body"]["ok"] is True, str(answer[-1:]))
    paidupd = {"update_id": 901, "message": {"message_id": 5, "from": {"id": buyer}, "chat": {"id": buyer},
                                             "successful_payment": {"currency": "XTR", "total_amount": stars,
                                                                    "invoice_payload": f"tx:{order_id}",
                                                                    "telegram_payment_charge_id": "ch_1"}}}
    r = c.post("/api/bot/webhook", json=paidupd, headers=W)
    st = c.get(f"/api/app/order/{order_id}", headers=U).json()
    check("e2e Stars order is paid", st.get("status") == "success", str(st))
    me2 = c.get("/api/app/me", headers=U).json()
    check("e2e subscription extended by the paid plan", me2["expires_at"] > exp_trial + 20 * 86400,
          f"{exp_trial} -> {me2['expires_at']}")
    r = c.post("/api/bot/webhook", json=paidupd, headers={"X-Telegram-Bot-Api-Secret-Token": "forged"})
    check("e2e forged webhook refused", r.status_code == 403)
    me3 = c.get("/api/app/me", headers=U).json()
    check("e2e repeated payment update not counted twice", me3["expires_at"] == me2["expires_at"])
    r = c.get("/admin")
    check("e2e admin page opens", r.status_code == 200 and "<html" in r.text.lower())
    r = c.get("/app")
    check("e2e mini app page opens", r.status_code == 200)


# ----------------------------------------------------------------- main
def prepare():
    WORK.mkdir(parents=True, exist_ok=True)
    FAKE.mkdir(parents=True, exist_ok=True)
    # A git repository to clone from, as GitHub would serve it.
    repo = WORK / "repo.git"
    if repo.exists():
        shutil.rmtree(repo)
    src = WORK / "src"
    shutil.rmtree(src, ignore_errors=True)
    shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(
        ".git", "data", ".env", ".dev", "__pycache__", ".pytest_cache", "*.pyc"))
    g = ["git", "-C", str(src)]
    subprocess.run(g + ["init", "-q", "-b", "main"], check=True)
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "test"], check=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(repo)], check=True)
    for f in (HERE / "bin").iterdir():
        f.chmod(0o755)
    fakes = subprocess.Popen([sys.executable, str(HERE / "fakes.py")],
                             env={**os.environ, "FAKE_DIR": str(FAKE)}, stdout=subprocess.PIPE, text=True)
    fakes.stdout.readline()
    return fakes


def guard(fn, *a):
    """A crashed scenario is a failure, not the end of the run."""
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        check(f"{fn.__name__} ran to the end", False, f"{type(e).__name__}: {e}")
        return None


def main(argv: list[str]) -> int:
    want = set(argv) or {"s1", "s2", "s3", "s3b", "s4", "s5", "s6", "s7", "s8", "s9", "e2e"}
    fakes = prepare()
    try:
        d1 = guard(s1_clean_domain_unattended) if want & {"s1", "s5", "s8", "s9", "e2e"} else None
        if "e2e" in want and d1:
            guard(e2e_owner_flow, d1)
        if "s5" in want and d1:
            guard(s5_update_and_status, d1)
        if "s8" in want and d1:
            guard(s8_payments_from_menu, d1)
        if "s9" in want and d1:
            guard(s9_update_from_github, d1)
        d2 = guard(s2_curl_pipe_tunnel_interactive) if want & {"s2", "s7"} else None
        if "s7" in want and d2:
            guard(s7_menu_switch_tunnel_to_domain, d2)
        if want & {"s3", "s6"}:
            r3 = guard(s3_existing_caddy_interactive)
            if "s6" in want and r3:
                guard(s6_uninstall_restores_caddyfile, *r3)
        if "s3b" in want:
            guard(s3b_broken_caddy_rolls_back)
        if "s4" in want:
            guard(s4_host_nginx)
    finally:
        stop_app()
        fakes.terminate()
    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)} passed, {len(bad)} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

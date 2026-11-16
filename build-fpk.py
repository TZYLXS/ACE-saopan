# -*- coding: utf-8 -*-
"""把 renhua 插件（CLI 报错翻译 + PyQt5 物理屏控制台 + 飞牛 Web 面板）打成 fnOS 的 fpk。

fpk 结构（与官方样例 autopower-1.0.4.fpk 对照）：
  外层 tar.gz:
    app.tgz            # 内层 tar.gz，解压到 TRIM_APPDEST
       RenHua/…        # 程序本体
       ui/config       # 桌面入口（icon / iframe / port）
       ui/images/…     # 图标
       config/*        # 权限与资源
    cmd/*              # 生命周期脚本
    config/{privilege,resource}
    ICON.PNG / ICON_256.PNG
    manifest           # key=value，checksum = md5(app.tgz)
"""
import gzip
import hashlib
import os
import shutil
import tarfile

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "src")
PKG = os.path.join(BASE, "pkg")
APP_TREE = os.path.join(BASE, "apptree")
ASSETS = os.path.join(BASE, "assets")
OUT = os.path.join(BASE, "dist", "扫盘.fpk")

APPNAME = "renhua"
VERSION = "1.1.0"
WEB_PORT = 8770
CTRL_PORT = 8771

MANIFEST = """appname                    = {appname}
version                    = {version}
display_name               = 存储体检
desc                       = 存储故障处置 + 报错人话翻译 + 物理屏直显。<br>含 <b>renhua</b> 命令（把 dmesg / journal 的存储报错翻成人话，给原文 + 人话 + 建议 + 严重度）、PyQt5 全自绘的物理屏控制台（F1-F6：帮助 / 坏块扫描 / 端口识别 / 健康体检 / 报错翻译 / 测速，直写 /dev/fb0，不依赖桌面）、以及本 Web 面板（同一个控制台的浏览器入口，可切页、起停扫描与测速）。<br>系统依赖：python3、python3-pyqt5、fonts-wqy-microhei、smartmontools、e2fsprogs（安装时自动补）。
arch                       = x86_64
platform                   = x86
source                     = thirdparty
maintainer                 = TZYLXS
distributor                = TZYLXS
desktop_uidir              = ui
desktop_applaunchname      = renhua.Application
service_port               = {port}
ctl_stop                   = true
checkport                  = false
disable_authorization_path = true
checksum                   = {checksum}
"""

PRIVILEGE = """{
    "defaults": {
        "run-as": "root"
    }
}
"""

RESOURCE = """{
    "data-share": {
        "shares": []
    }
}
"""

UI_CONFIG = """{
    ".url": {
        "renhua.Application": {
            "title": "存储体检",
            "desc": "报错人话翻译 / 坏块扫描 / 磁盘测速 / 物理屏定页",
            "icon": "images/icon_{0}.png",
            "type": "iframe",
            "protocol": "http",
            "port": "%d",
            "url": "/",
            "allUsers": true
        }
    }
}
""" % WEB_PORT

WIZARD_INSTALL = """[
    {
        "stepTitle": "安装前确认",
        "items": [
            {
                "type": "tips",
                "helpText": "存储体检会把画面直接画到服务器的物理显示器（/dev/fb0），所以需要以 root 身份运行；请只在本机手动安装使用。"
            },
            {
                "type": "tips",
                "helpText": "安装时会检查并自动补装系统依赖：python3、python3-pyqt5、fonts-wqy-microhei、smartmontools、e2fsprogs。补装日志在 /var/log/renhua-install.log，离线环境可能装不上，程序仍可先用 CLI 部分。"
            },
            {
                "type": "tips",
                "helpText": "应用会占用本机 8770 端口作为 Web 面板（应用图标点开就是它），物理屏控制台与控制通道走 127.0.0.1:8771，不对外暴露。"
            },
            {
                "type": "tips",
                "helpText": "物理屏键盘会被程序独占（保证 F1-F6 不被系统切走）。运行期间本机键盘只能操作这个控制台，退出或卸载后恢复。"
            },
            {
                "type": "switch",
                "field": "wizard_risk_acknowledged",
                "label": "我已了解：应用以 root 运行、独占本机键盘、会占用 8770 端口",
                "rules": [
                    {
                        "required": true,
                        "message": "请先确认已了解上述说明"
                    }
                ]
            }
        ]
    }
]
"""

CMD_MAIN = """#!/bin/bash
# renhua：start / stop / status / restart
# 起两个进程：物理屏控制台（/dev/fb0，PyQt5 linuxfb）+ Web 面板（本机 8770）
APPDEST="${TRIM_APPDEST:-}"
if [ -z "$APPDEST" ]; then
  APPDEST="$(cd "$(dirname "$0")/../target" 2>/dev/null && pwd)"
fi
APP="$APPDEST/RenHua"
CONF=/etc/renhua/renhua.conf
PIDF=/var/run/renhua-console.pid
WPIDF=/var/run/renhua-web.pid
LOGF=/var/log/renhua-console.log
WLOGF=/var/log/renhua-web.log
WEB_PORT=%d
CTRL_PORT=%d

alive() {
  [ -f "$1" ] || return 1
  p="$(tr -d '[:space:]' < "$1" 2>/dev/null)"
  [ -n "$p" ] && kill -0 "$p" 2>/dev/null
}

do_start() {
  page=5
  [ -r "$CONF" ] && . "$CONF"
  case "$page" in 1|2|3|4|5|6) ;; *) page=5 ;; esac

  mkdir -p /var/run /var/log /etc/renhua

  # 物理屏控制台
  if ! alive "$PIDF"; then
    SCRIPT="$APP/share/renhua-console.py"
    if [ ! -f "$SCRIPT" ]; then
      echo "missing $SCRIPT" >> "$LOGF"
    elif [ ! -w /dev/fb0 ]; then
      echo "$(date '+%%F %%T') /dev/fb0 不可写（无显示器或未开 framebuffer），跳过物理屏；Web 面板照常可用" >> "$LOGF"
    elif ! /usr/bin/python3 -c 'import PyQt5' >/dev/null 2>&1; then
      echo "$(date '+%%F %%T') PyQt5 缺失，跳过物理屏；Web 面板照常可用（装 python3-pyqt5 后重启应用即可）" >> "$LOGF"
    else
      RENHUA_CTRL_PORT="$CTRL_PORT" \\
        QT_QPA_PLATFORM="linuxfb:fb=/dev/fb0" QT_QPA_FONTDIR="$APP/share/fonts" \\
        setsid nohup /usr/bin/python3 "$SCRIPT" --page "$page" >> "$LOGF" 2>&1 &
      echo $! > "$PIDF"
    fi
  fi

  # Web 面板
  if ! alive "$WPIDF"; then
    SCRIPT="$APP/share/renhua-web.py"
    if [ -f "$SCRIPT" ]; then
      if ss -ltn 2>/dev/null | grep -q ":$WEB_PORT "; then
        echo "$(date '+%%F %%T') 端口 $WEB_PORT 已被占用，Web 面板未启动" >> "$WLOGF"
        echo "port $WEB_PORT busy" > "$WPIDF"
        rm -f "$WPIDF"
      else
        RENHUA_WEB_PORT="$WEB_PORT" RENHUA_CTRL_PORT="$CTRL_PORT" \\
          setsid nohup /usr/bin/python3 "$SCRIPT" --port "$WEB_PORT" >> "$WLOGF" 2>&1 &
        echo $! > "$WPIDF"
      fi
    else
      echo "missing $SCRIPT" >> "$WLOGF"
    fi
  fi

  sleep 2
  alive "$PIDF" || alive "$WPIDF"
}

do_stop() {
  alive "$PIDF" && kill "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null
  alive "$WPIDF" && kill "$(cat "$WPIDF" 2>/dev/null)" 2>/dev/null
  pkill -f "$APP/share/renhua-console.py" 2>/dev/null
  pkill -f "$APP/share/renhua-web.py" 2>/dev/null
  rm -f "$PIDF" "$WPIDF"
  return 0
}

case "${1:-}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; sleep 1; do_start ;;
  status)  alive "$PIDF" || alive "$WPIDF" ;;
  *) exit 1 ;;
esac
""" % (WEB_PORT, CTRL_PORT)

CMD_INSTALL_INIT = """#!/bin/bash
# 安装前：停掉早期手工部署的 systemd 守护，避免两个进程同时写 /dev/fb0
for u in renhua-console.service renhua-scanprog.service renhua-screen.service; do
  systemctl stop "$u" >/dev/null 2>&1 || true
  systemctl disable "$u" >/dev/null 2>&1 || true
done
exit 0
"""

CMD_INSTALL_CALLBACK = """#!/bin/bash
APPDEST="${TRIM_APPDEST:-}"
APP="$APPDEST/RenHua"
LOG=/var/log/renhua-install.log
: > "$LOG" 2>/dev/null || true

mkdir -p /etc/renhua /var/run /var/log /var/lib/renhua

# --- 依赖（缺什么补什么，装不上就记日志，程序仍能起 CLI 部分） ---
need=""
command -v python3 >/dev/null 2>&1 || need="$need python3"
python3 -c 'import PyQt5' >/dev/null 2>&1 || need="$need python3-pyqt5"
[ -f /usr/share/fonts/truetype/wqy/wqy-microhei.ttc ] || need="$need fonts-wqy-microhei"
command -v smartctl >/dev/null 2>&1 || need="$need smartmontools"
command -v badblocks >/dev/null 2>&1 || need="$need e2fsprogs"
if [ -n "$need" ]; then
  echo "installing:$need" >> "$LOG"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends $need >> "$LOG" 2>&1 \\
    || apt-get update -qq >> "$LOG" 2>&1 \\
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends $need >> "$LOG" 2>&1
fi

# --- 配置：默认物理屏停在 F5（报错翻译总览），Web 面板口 8770 ---
if [ ! -f /etc/renhua/renhua.conf ]; then
  cat > /etc/renhua/renhua.conf <<'CONF'
# 物理屏开机停在哪个页面：1..6（5 = F5 报错翻译）
page=5
CONF
fi

chmod 0755 "$APP/bin/renhua" "$APP/bin/renhua-screen" "$APP/bin/renhua-screen-mode" >/dev/null 2>&1 || true
ln -sf "$APP/bin/renhua" /usr/local/bin/renhua 2>/dev/null || true
ln -sf "$APP/bin/renhua-screen" /usr/local/bin/renhua-screen 2>/dev/null || true
ln -sf "$APP/bin/renhua-screen-mode" /usr/local/bin/renhua-screen-mode 2>/dev/null || true

"$(dirname "$0")/main" start >> "$LOG" 2>&1 || true
echo "install done: $(date '+%F %T')" >> "$LOG"
exit 0
"""

CMD_UNINSTALL = """#!/bin/bash
"$(dirname "$0")/main" stop >/dev/null 2>&1 || true
rm -f /usr/local/bin/renhua /usr/local/bin/renhua-screen /usr/local/bin/renhua-screen-mode
rm -f /var/run/renhua-console.pid /var/run/renhua-web.pid
rm -rf /etc/renhua
exit 0
"""

CMD_STUB = """#!/bin/bash
set -e
exit 0
"""

CMD_START_STOP = """#!/bin/bash
# 应用中心的启停按钮统一走 main
"$(dirname "$0")/main" "$@"
"""

SCREEN_MODE = """#!/bin/bash
# renhua-screen-mode —— 切换物理屏显示哪一页
#   renhua-screen-mode 1..6      F1 帮助 / F2 坏块扫描 / F3 端口识别 / F4 健康体检 / F5 报错翻译 / F6 测速
#   renhua-screen-mode status    查看当前配置与进程
CONF=/etc/renhua/renhua.conf

say_status() {
    page="$(grep '^page=' "$CONF" 2>/dev/null | cut -d= -f2)"
    [ -n "$page" ] || page=5
    pid="$(cat /var/run/renhua-console.pid 2>/dev/null)"
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        pid="$(pgrep -f 'renhua-console.py' 2>/dev/null | head -1)"
    fi
    web="$(cat /var/run/renhua-web.pid 2>/dev/null)"
    if [ -z "$web" ] || ! kill -0 "$web" 2>/dev/null; then
        web="$(pgrep -f 'renhua-web.py' 2>/dev/null | head -1)"
    fi
    out="物理屏页：F$page"
    if [ -n "$pid" ]; then
        out="$out　控制台：运行中（pid $pid）"
    else
        out="$out　控制台：未运行"
    fi
    if [ -n "$web" ]; then
        out="$out　Web 面板：http://<NAS>:8770（pid $web）"
    else
        out="$out　Web 面板：未运行"
    fi
    echo "$out"
}

case "${1:-status}" in
  1|2|3|4|5|6)
    mkdir -p /etc/renhua
    if [ -f "$CONF" ]; then
      sed -i "s/^page=.*/page=$1/" "$CONF"
      grep -q '^page=' "$CONF" || echo "page=$1" >> "$CONF"
    else
      printf 'page=%s\\n' "$1" > "$CONF"
    fi
    echo "已设为 F$1（下次启动控制台生效；当前画面请按键盘 F1-F6 或点 Web 面板）"
    ;;
  status) say_status ;;
  *)
    echo "用法：renhua-screen-mode 1|2|3|4|5|6|status"
    exit 1
    ;;
esac
"""

SCREEN_RUN = """#!/bin/bash
# renhua-screen —— 重启物理屏控制台（把画面重新画到 /dev/fb0）
MAIN="$(cd "$(dirname "$0")/../../cmd" 2>/dev/null && pwd)/main"
if [ ! -x "$MAIN" ]; then
  echo "找不到 cmd/main（请在应用目录内执行）"
  exit 1
fi
"$MAIN" restart
echo "物理屏已重启；Web 面板：http://<NAS>:8770"
"""

README = """renhua · 存储体检（报错翻译 + 物理屏控制台 + Web 面板）
=========================================================

一、Web 面板（飞牛应用中心里点图标打开）
   http://<NAS 地址>:8770
   - 磁盘与接口：型号 / SN / 容量 / 链路速率 / 温度 / 重映射 / 待映射 / 离线不可纠正 / CRC
   - 物理屏当前页：点 F1-F6 直接把服务器那块屏切过去
   - 坏块扫描：选盘 → 开始 / 停止，进度条 + 速度 + 错误数；扫完 0 坏块自动清该盘历史告警，
     有坏块直接给处置建议
   - 磁盘测速：一轮完整测试（顺序读 1GB / 顺序写 1GB / 小文件 32MB / 零碎 32MB / RND4K Q1T1）
   - 报错人话翻译：近 24 小时命中列表（等级 / 次数 / 人话 / 建议 / 原始样例）

二、物理屏（服务器直接接显示器，无桌面也能跑）
   键盘：F1-F6 切页，方向键选择磁盘，Enter 开始/停止扫描或测速，ESC 退出
   - F1 帮助 / F2 坏块扫描 / F3 端口识别 / F4 健康体检 / F5 报错翻译 / F6 测速

三、CLI
   dmesg | renhua                把报错翻成人话
   renhua --dmesg / --journal    直接读内核日志或 journal
   dmesg | renhua --top          按类别汇总
   renhua "ata3.00: error: { UNC }"
   renhua --rules                查看内置规则
   renhua-screen-mode 1..6       设定物理屏开机停在哪一页
   renhua-screen-mode status     查看进程与端口
   renhua-screen                 重启物理屏控制台

四、配置与依赖
   /etc/renhua/renhua.conf       物理屏默认页（page=5）
   /var/lib/renhua/verified.json 全盘校验通过的盘（自动隐去历史告警）
   依赖：python3、python3-pyqt5、fonts-wqy-microhei、smartmontools、e2fsprogs
   安装脚本会检查并自动补装（日志 /var/log/renhua-install.log）
   物理屏需要 root 写 /dev/fb0；Web 面板与控制台通信走本机 127.0.0.1:8771
"""


def write(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(path, mode)


def tar_add(t, path, arc, mode):
    """按显式权限加入 tar（Windows 上文件没有 exec 位，必须手写）"""
    ti = t.gettarinfo(path, arcname=arc)
    ti.mode = mode
    ti.uid = 0
    ti.gid = 0
    ti.uname = "root"
    ti.gname = "root"
    if ti.isreg():
        with open(path, "rb") as f:
            t.addfile(ti, f)
    else:
        t.addfile(ti)


def app_mode(arc):
    if arc.startswith(("RenHua/bin/", "RenHua/share/")) and (
            arc.endswith(".py") or "." not in os.path.basename(arc)):
        return 0o755
    return 0o644


def copytree(src, dst):
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def build():
    if os.path.isdir(APP_TREE):
        shutil.rmtree(APP_TREE)
    for name in ("cmd", "config"):
        p = os.path.join(PKG, name)
        if os.path.isdir(p):
            shutil.rmtree(p)
    for name in ("manifest", "app.tgz"):
        p = os.path.join(PKG, name)
        if os.path.isfile(p):
            os.remove(p)

    app_root = os.path.join(APP_TREE, "RenHua")
    share = os.path.join(app_root, "share")
    bindir = os.path.join(app_root, "bin")
    for d in (share, bindir):
        os.makedirs(d, exist_ok=True)

    # --- 1) CLI 翻译插件 ---
    shutil.copy2(os.path.join(SRC, "renhua"), os.path.join(bindir, "renhua"))
    os.chmod(os.path.join(bindir, "renhua"), 0o755)

    # --- 2) 物理屏控制台（PyQt5 linuxfb，唯一部署源） ---
    console_py = os.path.join(SRC, "renhua-console.py")
    if not os.path.isfile(console_py):
        raise SystemExit("缺少 src\\renhua-console.py，先同步再打包")
    shutil.copy2(console_py, os.path.join(share, "renhua-console.py"))
    os.chmod(os.path.join(share, "renhua-console.py"), 0o755)

    # --- 2b) 中文字体随包带走：全新机器没装 fonts-wqy-microhei 也能显示中文 ---
    font_dir = os.path.join(share, "fonts")
    os.makedirs(font_dir, exist_ok=True)
    font_src = os.path.join(ASSETS, "wqy-microhei.ttc")
    if os.path.isfile(font_src):
        shutil.copy2(font_src, os.path.join(font_dir, "wqy-microhei.ttc"))
        os.chmod(os.path.join(font_dir, "wqy-microhei.ttc"), 0o644)
        notice = os.path.join(ASSETS, "FONT-NOTICE.txt")
        if os.path.isfile(notice):
            shutil.copy2(notice, os.path.join(font_dir, "FONT-NOTICE.txt"))
    else:
        print("WARN: 缺 assets\\wqy-microhei.ttc，包里不带字体")

    # --- 3) Web 面板 ---
    web_py = os.path.join(SRC, "renhua-web.py")
    if not os.path.isfile(web_py):
        raise SystemExit("缺少 src\\renhua-web.py")
    shutil.copy2(web_py, os.path.join(share, "renhua-web.py"))
    os.chmod(os.path.join(share, "renhua-web.py"), 0o755)
    web_dir = os.path.join(app_root, "web")
    copytree(os.path.join(SRC, "web"), web_dir)
    for root, _dirs, files in os.walk(web_dir):
        for f in files:
            os.chmod(os.path.join(root, f), 0o644)

    # --- 4) 桌面入口 ui/（放在 app 根，安装后即 $TRIM_APPDEST/ui/config） ---
    ui_dir = os.path.join(APP_TREE, "ui")
    copytree(os.path.join(SRC, "ui"), ui_dir)
    write(os.path.join(ui_dir, "config"), UI_CONFIG)
    for root, _dirs, files in os.walk(ui_dir):
        for f in files:
            os.chmod(os.path.join(root, f), 0o644)

    # --- 4b) 安装向导 wizard/install（包根，不属于 app） ---
    write(os.path.join(PKG, "wizard", "install"), WIZARD_INSTALL, 0o644)

    # --- 5) 命令脚本与说明 ---
    write(os.path.join(bindir, "renhua-screen-mode"), SCREEN_MODE, 0o755)
    write(os.path.join(bindir, "renhua-screen"), SCREEN_RUN, 0o755)
    write(os.path.join(app_root, "README.txt"), README)
    # 底版渲染脚本不再需要，留一份说明避免误找
    write(os.path.join(app_root, "旧版说明.txt"),
          "旧的 BMP 推屏实现（PowerShell 渲染 + fbshow + scanprog）已由 PyQt5 控制台取代，"
          "本包不再包含。需要恢复请用源码仓里的对应文件。\n")

    # --- 6) 内层 config ---
    write(os.path.join(APP_TREE, "config", "privilege"), PRIVILEGE)
    write(os.path.join(APP_TREE, "config", "resource"), RESOURCE)

    # --- 7) 外层 cmd / config ---
    cmds = {
        "main": (CMD_MAIN, 0o755),
        "install": (CMD_STUB, 0o755),
        "install_init": (CMD_INSTALL_INIT, 0o755),
        "install_callback": (CMD_INSTALL_CALLBACK, 0o755),
        "uninstall": (CMD_UNINSTALL, 0o755),
        "uninstall_init": (CMD_STUB, 0o755),
        "uninstall_callback": (CMD_STUB, 0o755),
        "upgrade_init": (CMD_STUB, 0o755),
        "upgrade_callback": (CMD_STUB, 0o755),
        "config_init": (CMD_STUB, 0o755),
        "config_callback": (CMD_STUB, 0o755),
        "start_stop_restart": (CMD_START_STOP, 0o755),
    }
    for name, (text, mode) in cmds.items():
        write(os.path.join(PKG, "cmd", name), text, mode)
    write(os.path.join(PKG, "config", "privilege"), PRIVILEGE)
    write(os.path.join(PKG, "config", "resource"), RESOURCE)

    # --- 8) app.tgz ---
    app_tgz_path = os.path.join(PKG, "app.tgz")
    with tarfile.open(app_tgz_path, "w:gz", format=tarfile.GNU_FORMAT) as t:
        for root, dirs, files in os.walk(APP_TREE):
            dirs.sort()
            files.sort()
            rel = os.path.relpath(root, APP_TREE).replace("\\", "/")
            if rel != ".":
                tar_add(t, root, rel, 0o755)
            for f in files:
                full = os.path.join(root, f)
                arc = f if rel == "." else rel + "/" + f
                tar_add(t, full, arc, app_mode(arc))

    checksum = hashlib.md5(open(app_tgz_path, "rb").read()).hexdigest()
    write(os.path.join(PKG, "manifest"),
          MANIFEST.format(appname=APPNAME, version=VERSION, port=WEB_PORT, checksum=checksum))

    # --- 9) 外层 fpk ---
    fpk_tar = os.path.join(PKG, "payload.tar")
    with tarfile.open(fpk_tar, "w", format=tarfile.GNU_FORMAT) as t:
        tar_add(t, app_tgz_path, "app.tgz", 0o644)
        for sub in ("cmd", "config"):
            tar_add(t, os.path.join(PKG, sub), sub, 0o755)
            for f in sorted(os.listdir(os.path.join(PKG, sub))):
                p = os.path.join(PKG, sub, f)
                if os.path.isfile(p):
                    tar_add(t, p, sub + "/" + f, 0o755 if sub == "cmd" else 0o644)
        for icon in ("ICON.PNG", "ICON_256.PNG"):
            tar_add(t, os.path.join(PKG, icon), icon, 0o644)
        tar_add(t, os.path.join(PKG, "wizard"), "wizard", 0o755)
        for f in sorted(os.listdir(os.path.join(PKG, "wizard"))):
            tar_add(t, os.path.join(PKG, "wizard", f), "wizard/" + f, 0o644)
        tar_add(t, os.path.join(PKG, "manifest"), "manifest", 0o644)
    raw = open(fpk_tar, "rb").read()
    os.remove(fpk_tar)
    with open(OUT, "wb") as f:
        f.write(gzip.compress(raw, 9, mtime=0))

    print("FPK=%s  size=%d  md5(app.tgz)=%s" % (OUT, os.path.getsize(OUT), checksum))
    print("app.tgz=%d" % os.path.getsize(app_tgz_path))


if __name__ == "__main__":
    build()

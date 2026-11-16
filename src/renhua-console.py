#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""renhua-console —— 存储体检控制台（Qt 全屏，F1-F5 分页，参考 MHDD 热键风格）

页面：
  F1 帮助        快捷键与说明
  F2 表面扫描    读校验进度条（badblocks 日志实时解析）
  F3 端口识别    6 个 SATA 口：型号 / 容量 / 链路速率 / 温度
  F4 健康体检    SMART 关键属性 + 结论
  F5 报错翻译    renhua 规则命中聚合（近 24 小时）

设计要点：
  - Qt5 的 linuxfb 平台插件直接画到 /dev/fb0，中文用文泉驿微米黑现场渲染，无需预生成位图。
  - 键盘直接读 /dev/input/eventN 并用 EVIOCGRAB 独占：这样按 F1-F5 不会触发内核 VT 切换，
    代价是本程序运行期间控制台键盘不可用（管理走 SSH）。可用 RENHUA_NO_GRAB=1 关闭独占。
  - 每 10 秒写 fb blank=0 / DPMS=On，防止 10 分钟息屏。
"""
import glob
import json
import mmap
import os
import queue
import random
import re
import shutil
import signal
import socketserver
import struct
import subprocess
import sys
import threading
import time

# ---- 必须在 import PyQt5 之前设置平台，否则会去找 X11 ----
if not os.environ.get("QT_QPA_PLATFORM"):
    os.environ["QT_QPA_PLATFORM"] = "linuxfb:fb=/dev/fb0"

# 中文字体：优先用包内自带的（全新机器没装 fonts-wqy-microhei 时也能显示中文）
_HERE = os.path.dirname(os.path.abspath(__file__))
_BUNDLE_FONT_DIR = os.path.join(_HERE, "fonts")
if os.path.isfile(os.path.join(_BUNDLE_FONT_DIR, "wqy-microhei.ttc")):
    os.environ.setdefault("QT_QPA_FONTDIR", _BUNDLE_FONT_DIR)
else:
    os.environ.setdefault("QT_QPA_FONTDIR", "/usr/share/fonts/truetype/wqy")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")

from PyQt5.QtCore import QRect, QTimer, Qt, QPointF  # noqa: E402
from PyQt5.QtGui import (QColor, QFont, QFontDatabase, QFontMetrics, QImage, QPainter,  # noqa: E402
                         QPen, QBrush, QLinearGradient, QPolygonF)
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

W, H = 1920, 1080
# 设计稿按 1920x1080 画；实际帧缓冲比这小时整体等比缩放，避免换台机器布局被切掉
FBW, FBH = W, H


def detect_fb_size():
    """读 /dev/fb0 的真实分辨率，读不到就按 1920x1080。"""
    import glob as _g
    for p in _g.glob("/sys/class/graphics/fb*/virtual_size"):
        try:
            txt = open(p).read().strip()
            w, h = [int(x) for x in txt.replace(" ", "").split(",")[:2]]
            if w > 0 and h > 0:
                return w, h
        except Exception:
            continue
    return W, H

# ---------- 配色 ----------
C_BG = QColor(13, 15, 19)
C_PANEL = QColor(22, 26, 34)
C_PANEL2 = QColor(18, 21, 28)
C_LINE = QColor(42, 46, 56)
C_TEXT = QColor(232, 234, 239)
C_DIM = QColor(154, 163, 178)
C_DIM2 = QColor(108, 116, 132)
C_GREEN = QColor(92, 214, 148)
C_ORANGE = QColor(255, 169, 77)
C_RED = QColor(255, 96, 96)
C_BLUE = QColor(110, 168, 255)
C_ACCENT = QColor(90, 130, 255)

FONT_FAMILY = "WenQuanYi Micro Hei"

PAGES = [
    ("F1", "帮助", "HELP"),
    ("F2", "坏块扫描", "SCAN"),
    ("F3", "端口识别", "PORTS"),
    ("F4", "健康体检", "SMART"),
    ("F5", "报错翻译", "ERRORS"),
    ("F6", "测速", "SPEED"),
]

ATALOG = "/var/log/bb500.log"
RENHUA_SRC_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "renhua"),
    "/usr/local/share/renhua/bin/renhua",
    "/usr/local/bin/renhua",
    "/usr/local/share/renhua/renhua",
)
HOST_TAG = "10.5.4.70"


# =====================  数据采集  =====================
def sh(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
        return (r.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""


def disk_rows():
    """返回每个 sdX 的字典：name/model/sn/size_gb/ata/spd/state"""
    out = []
    for name in sorted(os.listdir("/sys/block")):
        if not re.match(r"^sd[a-z]+$", name):
            continue
        base = "/sys/block/" + name
        real = os.path.realpath(base)
        m = re.search(r"/ata(\d+)/", real)
        ata = int(m.group(1)) if m else None

        def rd(p, default=""):
            try:
                return open(p, "r", errors="replace").read().strip() or default
            except OSError:
                return default

        model = rd(base + "/device/model") or rd(base + "/device/name")
        vendor = rd(base + "/device/vendor")
        sn = rd(base + "/device/serial")
        try:
            sectors = int(rd(base + "/size", "0"))
        except ValueError:
            sectors = 0
        size_gb = sectors * 512 / 1e9
        spd = "?"
        if ata:
            spd = rd("/sys/class/ata_link/link%d/sata_spd" % ata, "?")
        ro = rd(base + "/ro", "0") == "1"
        out.append({"name": name, "ata": ata, "model": (vendor + " " + model).strip(),
                    "sn": sn, "size_gb": size_gb, "spd": spd, "ro": ro})
    out.sort(key=lambda d: (d["ata"] or 99))
    return out


def link_speeds():
    spd = {}
    for i in range(1, 7):
        try:
            v = open("/sys/class/ata_link/link%d/sata_spd" % i).read().strip()
        except OSError:
            v = ""
        spd[i] = v if v and v != "<unknown>" else ""
    return spd


SMART_KEYS = {
    5: "重映射扇区", 9: "通电小时", 194: "温度", 197: "待映射扇区",
    198: "离线不可纠正", 199: "接口 CRC",
}


def smart_read(dev):
    """smartctl -A -H -i 解析关键属性 + 完整型号/序列号；不支持则返回 None。"""
    txt = sh("smartctl -A -H -i /dev/%s 2>/dev/null" % dev, timeout=25)
    if not txt or "Unknown USB bridge" in txt or "Unable to detect device type" in txt:
        return None
    vals = {}
    for line in txt.splitlines():
        low = line.lower()
        if low.startswith("device model:") or low.startswith("product:"):
            vals["_model"] = line.split(":", 1)[1].strip()
        elif low.startswith("serial number:") or low.startswith("serial no:"):
            vals["_sn"] = line.split(":", 1)[1].strip()
        parts = line.split()
        if len(parts) < 10 or not parts[0].isdigit():
            continue
        attr = int(parts[0])
        if attr not in SMART_KEYS:
            continue
        m = re.match(r"^(-?\d+)", parts[9])
        if m:
            vals[SMART_KEYS[attr]] = int(m.group(1))
    health = ""
    for line in txt.splitlines():
        if line.lower().startswith("smart overall-health"):
            health = line.split(":")[-1].strip()
    if not vals and not health:
        return None
    vals["_health"] = health
    return vals


def smart_verdict(v):
    if v is None:
        return ("无 SMART", C_DIM2)
    bad = []
    if v.get("待映射扇区", 0) > 0:
        bad.append(v["待映射扇区"])
    if v.get("离线不可纠正", 0) > 0:
        bad.append(v["离线不可纠正"])
    if v.get("重映射扇区", 0) > 0:
        bad.append(v["重映射扇区"])
    if bad:
        return ("有坏扇区", C_RED)
    if v.get("接口 CRC", 0) > 0:
        return ("线缆/接口可疑", C_ORANGE)
    if v.get("_health", "").upper().startswith("PASS"):
        return ("健康", C_GREEN)
    return ("正常", C_GREEN)


SCAN_LOCK = threading.Lock()
SPEED_LOCK = threading.Lock()
SCAN_PROC = {"p": None, "dev": None, "log": None, "t0": None}
LAST_DEV = {"dev": ""}


def root_disk_names():
    """/ 挂载在哪块盘上（这块盘不能拿来扫）。"""
    names = set()
    try:
        for line in open("/proc/mounts"):
            p = line.split()
            if len(p) >= 2 and p[1] == "/":
                m = re.match(r"/dev/(sd[a-z]+)", p[0])
                if m:
                    names.add(m.group(1))
    except OSError:
        pass
    return names


def scan_candidates(disks):
    roots = root_disk_names()
    return [d for d in disks if d["name"] not in roots]


def proc_alive(pid):
    """僵尸进程（Z）不算在跑。"""
    try:
        stat = open("/proc/%d/stat" % pid).read()
        return stat.rsplit(") ", 1)[-1][0] != "Z"
    except OSError:
        return False


def badblocks_pid():
    out = sh("pgrep -x badblocks || true", 8).strip()
    for tok in out.split():
        try:
            pid = int(tok)
        except ValueError:
            continue
        if proc_alive(pid):
            return pid
    p = SCAN_PROC.get("p")
    if p is not None and p.poll() is not None:   # 回收已退出的子进程，避免僵尸
        SCAN_PROC["p"] = None
    return None


def dev_of_pid(pid):
    try:
        cmd = open("/proc/%d/cmdline" % pid, "rb").read().split(b"\0")
    except OSError:
        return None
    for a in cmd:
        s = a.decode("utf-8", "replace")
        if s.startswith("/dev/sd"):
            return s
    return None


def scan_log_path(devname):
    if SCAN_PROC["log"] and os.path.exists(SCAN_PROC["log"]):
        return SCAN_PROC["log"]
    for cand in ("/var/log/bb-%s.log" % devname, ATALOG):
        if os.path.exists(cand):
            return cand
    try:
        logs = glob.glob("/var/log/bb*.log")
        if logs:
            return max(logs, key=os.path.getmtime)
    except OSError:
        pass
    return ATALOG


def start_scan(dev):
    """只读校验（badblocks -sv）。返回 (ok, msg)。"""
    with SCAN_LOCK:
        if badblocks_pid():
            return False, "已有扫描在运行，先停止"
        if not re.match(r"^/dev/sd[a-z]+$", dev or ""):
            return False, "非法设备名"
        devname = dev.split("/")[-1]
        if devname in root_disk_names():
            return False, "系统盘不参与扫描"
        log = "/var/log/bb-%s.log" % devname
        try:
            fh = open(log, "w")
        except OSError as ex:
            return False, "日志打不开（%s）" % type(ex).__name__
        try:
            p = subprocess.Popen(["badblocks", "-sv", "-b", "4096", dev],
                                 stdout=fh, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        except Exception as ex:
            fh.close()
            return False, "启动失败（%s）" % type(ex).__name__
        SCAN_PROC.update({"p": p, "dev": dev, "log": log, "t0": time.time()})
        LAST_DEV["dev"] = dev
        SNAP.set(msg="已开始扫描 %s" % dev, msg_time=time.time())
        return True, "已开始扫描 %s" % dev


def stop_scan():
    with SCAN_LOCK:
        pid = badblocks_pid()
        if not pid:
            return False, "当前没有扫描在跑"
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                return False, "停止失败"
        for _ in range(25):
            time.sleep(0.2)
            if not badblocks_pid():
                break
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                pass
        SNAP.set(msg="已停止扫描", msg_time=time.time())
        return True, "已停止扫描"


def scan_state():
    """解析 badblocks 进度日志（支持任意设备/任意日志）。"""
    pid = badblocks_pid()
    running = pid is not None
    live = dev_of_pid(pid) if pid else None
    if live:
        LAST_DEV["dev"] = live
    dev = live or SCAN_PROC.get("dev") or LAST_DEV["dev"] or ""
    if not dev:
        # 本进程没发起过扫描、也没有外部扫描在跑 → 没有可显示的任务
        return {"running": False, "pid": None, "pct": None, "elapsed": None,
                "errs": None, "dev": "", "log": "", "interrupted": False}
    devname = dev.split("/")[-1]
    log = scan_log_path(devname)
    st = {"running": running, "pid": pid, "pct": None, "elapsed": None,
          "errs": None, "dev": dev, "log": log}
    try:
        raw = open(log, "r", errors="replace").read()[-800:]
    except OSError:
        return st
    text = raw.replace("\r", "\n")
    p = re.findall(r"([0-9]+(?:\.[0-9]+)?)% done", text)
    if p:
        st["pct"] = float(p[-1])
    e = re.findall(r"([0-9]+:[0-9:]+) elapsed", text)
    if e:
        parts = [int(x) for x in e[-1].split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        st["elapsed"] = parts[0] * 3600 + parts[1] * 60 + parts[2]
    er = re.findall(r"\(([0-9]+)/([0-9]+)/([0-9]+) errors\)", text)
    if er:
        st["errs"] = "/".join(er[-1])
    if "Interrupted at block" in text or "中断" in text:
        st["interrupted"] = True
    return st


# ---------- 扫描结果：通过则清告警，有问题给建议 ----------
VERIFY_FILE = "/var/lib/renhua/verified.json"


def load_verified():
    try:
        with open(VERIFY_FILE, "r") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def save_verified(v):
    try:
        os.makedirs(os.path.dirname(VERIFY_FILE), exist_ok=True)
        with open(VERIFY_FILE, "w") as fh:
            json.dump(v, fh, ensure_ascii=False)
    except OSError:
        pass


def filter_verified(hits, disks, verified):
    """把“已全盘校验通过”的盘的历史告警从列表里去掉。"""
    if not verified:
        return hits
    toks = []
    for d in disks:
        if d["name"] not in verified:
            continue
        toks.append(d["name"].lower())
        if d.get("sn"):
            toks.append(d["sn"].lower())
        if d.get("ata"):
            toks.append("ata%d" % d["ata"])
    out = []
    for h in hits:
        text = ("%s %s %s" % (h.get("title", ""), h.get("sample", ""), h.get("human", ""))).lower()
        if any(t and t in text for t in toks):
            continue
        out.append(h)
    return out


def cover_ts_of(verified):
    """最近一次“0 坏块”全盘校验的完成时间。"""
    ts = [int(v.get("ts") or 0) for v in (verified or {}).values() if isinstance(v, dict)]
    return max(ts) if ts else 0


def annotate_hits(hits, verified):
    """不删历史告警，改成标注：最后出现早于校验完成 = 已被那次 0 坏块扫描覆盖。"""
    cts = cover_ts_of(verified)
    for h in hits:
        last = int(h.get("last_ts") or 0)
        h["cover_ts"] = cts
        h["covered"] = bool(cts and last and last <= cts)
    return hits


def advice_for(bad):
    """按坏块数量给处置建议。"""
    if bad <= 0:
        return ""
    if bad <= 8:
        return ("发现 %d 个坏块：先备份这块盘上的数据；再用 badblocks -n 重写或让文件系统标掉这些块；"
                "盘还能用，但不要放唯一副本。" % bad)
    if bad <= 50:
        return ("发现 %d 个坏块，已经在扩散：尽快把数据迁到其它盘，不要继续往里写新数据，"
                "迁完建议换盘。" % bad)
    return ("发现 %d 个坏块，盘已不可靠：立刻停止向它写入，先抢救数据，然后更换硬盘。" % bad)


def finish_scan(dev):
    """badblocks 结束后判定：0 坏块 → 记录“已校验通过”并清掉该盘告警；有坏块 → 出建议。"""
    devname = (dev or "").split("/")[-1]
    log = scan_log_path(devname) if devname else ""
    try:
        text = open(log, "r", errors="replace").read()
    except OSError:
        return
    m = re.search(r"Pass completed,\s*([0-9]+) bad blocks found", text)
    er = re.findall(r"\(([0-9]+)/([0-9]+)/([0-9]+) errors\)", text)
    nerr = sum(int(x) for x in er[-1]) if er else 0
    if not m:
        # 没扫完（被停止/中断）→ 什么都不做
        if "Interrupted" in text:
            SNAP.set(scan_advice=None, msg="扫描被中断，结果不完整（badblocks 不支持续扫）",
                     msg_time=time.time())
        return
    bad = int(m.group(1))
    if bad == 0 and nerr == 0:
        d = next((x for x in SNAP.get().get("disks", []) if x["name"] == devname), None)
        v = load_verified()
        v[devname] = {"ts": int(time.time()), "bad": 0,
                      "sn": (d or {}).get("sn", ""), "model": (d or {}).get("model", "")}
        save_verified(v)
        SNAP.set(verified=v, scan_advice=None,
                 msg="全盘校验通过（0 坏块）：已清除该盘的历史告警", msg_time=time.time())
    else:
        SNAP.set(scan_advice=advice_for(max(bad, nerr)),
                 msg="扫描发现坏块，结果需要处理", msg_time=time.time())


# ---------- F6 测速：一轮完整测试 ----------
# 顺序读 1GB / 顺序写 1GB / 小文件写读 32MB / 零碎文件写读 32MB / RND4K Q1T1 读写
SPEED_LOCK = threading.Lock()
SPEED_ABORT = threading.Event()
SPEED_PID = {"p": None}

STAGES = [
    ("seq_read", "顺序读 1GB", 1024 * 1024 * 1024),
    ("seq_write", "顺序写 1GB", 1024 * 1024 * 1024),
    ("small_write", "小文件写 32MB（1M×32）", 32 * 1024 * 1024),
    ("small_read", "小文件读 32MB", 32 * 1024 * 1024),
    ("frag_write", "零碎写 32MB（4K×8192）", 32 * 1024 * 1024),
    ("frag_read", "零碎读 32MB", 32 * 1024 * 1024),
    ("rnd_read", "RND4K Q1T1 读", 8 * 1024 * 1024),
    ("rnd_write", "RND4K Q1T1 写", 8 * 1024 * 1024),
]
RND_OPS = 2000          # 4K 随机次数（2000×4K ≈ 8MB）

SPEED_STATE = {"running": False, "dev": "", "devname": "", "mount": "", "dir": "",
               "stage_i": 0, "stage": "空闲", "pct": 0.0, "cur": None, "unit": "MB/s",
               "results": [], "err": "", "log": "", "elapsed": 0.0}


class SpeedAbort(Exception):
    pass


def _slave_disks(node, out, depth=0):
    """递归解析 /sys/block/<node>/slaves，收出底层的真实磁盘名（sdb / sdc ...）。"""
    if depth > 4:
        return
    try:
        sl = sorted(os.listdir(node + "/slaves"))
    except OSError:
        return
    for s in sl:
        base = re.sub(r"[0-9]+$", "", s).split("p")[0]
        if base.startswith(("sd", "hd", "nvme", "vd")):
            out.add(base)
        else:
            _slave_disks("/sys/block/" + s, out, depth + 1)


def mount_for(devname):
    """找到这块盘挂载在哪个目录（盘上没文件系统就返回空串）。
    fnOS 的存储空间可能落在 dm → md → sd 多层，这里逐层拆。"""
    mounts = []
    try:
        for line in open("/proc/mounts"):
            p = line.split()
            if len(p) >= 2 and p[0].startswith("/dev/"):
                mounts.append((p[0], p[1]))
    except OSError:
        return ""
    for src, mnt in mounts:
        if re.match(r"^/dev/%s(\d+|p\d+)?$" % devname, src):
            return mnt
    for dm in sorted(glob.glob("/sys/block/dm-*")):
        try:
            nm = open(dm + "/dm/name").read().strip()
        except OSError:
            continue
        disks = set()
        _slave_disks(dm, disks)
        if devname not in disks:
            continue
        for src, mnt in mounts:
            if src.startswith("/dev/mapper/"):
                base = src.split("/")[-1]
                if base == nm or base.rsplit("-", 1)[0] == nm:
                    return mnt
    # 直接挂在 /dev/mdX 上的情况
    for md in sorted(glob.glob("/sys/block/md*")):
        disks = set()
        _slave_disks(md, disks)
        if devname not in disks:
            continue
        node = "/dev/" + md.split("/")[-1]
        for src, mnt in mounts:
            if re.match(r"^%s(\d+|p\d+)?$" % node, src):
                return mnt
    return ""


def _speed_push(**kw):
    with SPEED_LOCK:
        SPEED_STATE.update(kw)
        snap = dict(SPEED_STATE)
    SNAP.set(speed=snap)


def _speed_stage(i, frac=1.0, cur=None, unit="MB/s"):
    frac = max(0.0, min(1.0, frac))
    _speed_push(stage_i=i, stage=STAGES[i][1],
                pct=(i + frac) * 100.0 / len(STAGES), cur=cur, unit=unit)


def _mb(v, unit="MB/s"):
    return "跳过" if v is None else "%.1f %s" % (v, unit)


def _run_dd(i, args, log, total):
    """跑一个 dd 阶段，实时解析进度；被停止或超时返回 None。"""
    try:
        fh = open(log, "w")
    except OSError as ex:
        _speed_push(err="日志打不开（%s）" % type(ex).__name__)
        return None
    p = subprocess.Popen(args, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    SPEED_PID["p"] = p
    t0 = lastt = time.time()
    lastn = 0
    while p.poll() is None:
        if SPEED_ABORT.is_set():
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except OSError:
                pass
            try:
                p.wait(8)
            except Exception:
                p.kill()
            SPEED_PID["p"] = None
            fh.close()
            return None
        if time.time() - t0 > 1200:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except OSError:
                pass
            break
        time.sleep(0.4)
        try:
            txt = open(log, "r", errors="replace").read()[-300:]
        except OSError:
            continue
        b = re.findall(r"([0-9]+) bytes", txt)
        n = int(b[-1]) if b else 0
        if n and time.time() - lastt > 0.8:
            _speed_stage(i, n * 1.0 / total, (n - lastn) / (time.time() - lastt) / 1e6)
            lastn, lastt = n, time.time()
    rc = p.wait()
    SPEED_PID["p"] = None
    fh.close()
    secs = time.time() - t0
    _speed_stage(i, 1.0)
    if rc != 0:
        return None
    return total / max(0.001, secs) / 1e6


def _read_direct(path, size):
    """绕页缓存读（O_DIRECT + 页对齐缓冲），失败退回普通 read。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECT)
    except OSError:
        with open(path, "rb") as fh:
            fh.read(size)
        return
    buf = mmap.mmap(-1, 4096)
    try:
        off = 0
        while off < size:
            os.preadv(fd, [buf], off)
            off += 4096
    finally:
        buf.close()
        os.close(fd)


def _small_files(i, d, count, size, write):
    """count×size 的小文件读写（每个文件 fsync，模拟真实小文件写入）。"""
    t0 = time.time()
    total = count * size
    chunk = b"\0" * size
    done = 0
    for k in range(count):
        if SPEED_ABORT.is_set():
            raise SpeedAbort()
        path = os.path.join(d, "s%03d.bin" % k)
        if write:
            with open(path, "wb") as fh:
                fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
        else:
            if os.path.exists(path):
                _read_direct(path, size)
        done += size
        _speed_stage(i, done * 1.0 / total, done / max(0.001, time.time() - t0) / 1e6)
    try:
        os.sync()
    except OSError:
        pass
    return total / max(0.001, time.time() - t0) / 1e6


def _frag_files(i, d, count, size, write):
    """海量 4K 零碎文件读写（考验元数据与 4K 随机）。"""
    t0 = time.time()
    total = count * size
    chunk = b"\0" * size
    done = 0
    for k in range(count):
        if SPEED_ABORT.is_set():
            raise SpeedAbort()
        path = os.path.join(d, "f", "%04d" % (k // 100), "%05d" % k)
        if write:
            if k % 100 == 0:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(chunk)
        else:
            try:
                _read_direct(path, size)
            except OSError:
                pass
        done += size
        if k % 256 == 0:
            _speed_stage(i, done * 1.0 / total, done / max(0.001, time.time() - t0) / 1e6)
    _speed_stage(i, 1.0)
    return total / max(0.001, time.time() - t0) / 1e6


def _rnd4k(i, path, write):
    """RND4K Q1T1：单线程、队列深度 1、4K 随机读写，返回 (MB/s, IOPS)。"""
    buf = mmap.mmap(-1, 4096)
    try:
        fd = os.open(path, os.O_RDWR | os.O_DIRECT)
        direct = True
    except OSError:
        fd = os.open(path, os.O_RDWR)
        direct = False
    size = os.fstat(fd).st_size
    blocks = max(1, size // 4096)
    offs = [random.randrange(blocks) * 4096 for _ in range(256)]
    t0 = time.time()
    for k in range(RND_OPS):
        if SPEED_ABORT.is_set():
            os.close(fd)
            buf.close()
            raise SpeedAbort()
        off = offs[k & 255] if k % 256 == 0 else random.randrange(blocks) * 4096
        if write:
            os.pwritev(fd, [buf], off)
        else:
            os.preadv(fd, [buf], off)
        if k % 64 == 0:
            _speed_stage(i, (k + 1) * 1.0 / RND_OPS, None)
    try:
        os.fsync(fd)
    except OSError:
        pass
    os.close(fd)
    buf.close()
    secs = time.time() - t0
    iops = RND_OPS / max(0.001, secs)
    st = SPEED_STATE
    _speed_push(direct=direct)
    return iops * 4096 / 1e6, iops


def speed_worker(dev, devname, mount, log):
    d = os.path.join(mount, ".renhua-speed")
    seq = os.path.join(d, "seq1g.bin")
    results = []
    t0 = time.time()
    try:
        os.makedirs(d, exist_ok=True)
        _speed_push(dir=d, results=[], err="", elapsed=0.0)
        r = []
        for i, (_k, name, total) in enumerate(STAGES):
            _speed_stage(i, 0.0)
            if SPEED_ABORT.is_set():
                raise SpeedAbort()
            if _k == "seq_read":
                v = _run_dd(i, ["dd", "if=" + dev, "of=/dev/null", "bs=1M", "count=1024",
                                "iflag=direct", "status=progress"], log, total)
                r.append((name, _mb(v)))
            elif _k == "seq_write":
                v = _run_dd(i, ["dd", "if=/dev/zero", "of=" + seq, "bs=1M", "count=1024",
                                "conv=fsync", "status=progress"], log, total)
                r.append((name, _mb(v)))
            elif _k == "small_write":
                r.append((name, _mb(_small_files(i, d, 32, 1024 * 1024, True))))
            elif _k == "small_read":
                r.append((name, _mb(_small_files(i, d, 32, 1024 * 1024, False))))
            elif _k == "frag_write":
                r.append((name, _mb(_frag_files(i, d, 8192, 4096, True))))
            elif _k == "frag_read":
                r.append((name, _mb(_frag_files(i, d, 8192, 4096, False))))
            elif _k == "rnd_read":
                if not os.path.exists(seq):
                    _run_dd(i, ["dd", "if=/dev/zero", "of=" + seq, "bs=1M", "count=64",
                                "conv=fsync"], log, 64 * 1024 * 1024)
                mb, iops = _rnd4k(i, seq, False)
                r.append((name, "%.1f MB/s · %.0f IOPS" % (mb, iops)))
            elif _k == "rnd_write":
                mb, iops = _rnd4k(i, seq, True)
                r.append((name, "%.1f MB/s · %.0f IOPS" % (mb, iops)))
            _speed_push(results=list(r), elapsed=time.time() - t0)
        _speed_push(running=False, stage="测试完成", pct=100.0, cur=None,
                    results=list(r), elapsed=time.time() - t0)
        SNAP.set(msg="测速完成：%s（临时文件已清理）" % dev, msg_time=time.time())
    except SpeedAbort:
        _speed_push(running=False, stage="已停止", cur=None, elapsed=time.time() - t0)
        SNAP.set(msg="测速已停止（临时文件已清理）", msg_time=time.time())
    except Exception as ex:
        _speed_push(running=False, stage="出错", err="%s: %s" % (type(ex).__name__, ex),
                    cur=None, elapsed=time.time() - t0)
        SNAP.set(msg="测速出错：%s" % type(ex).__name__, msg_time=time.time())
    finally:
        SPEED_PID["p"] = None
        shutil.rmtree(d, ignore_errors=True)


def start_speed(dev):
    if SPEED_STATE.get("running"):
        return False, "测速已在进行"
    if not re.match(r"^/dev/sd[a-z]+$", dev or ""):
        return False, "非法设备名"
    devname = dev.split("/")[-1]
    if devname in root_disk_names():
        return False, "系统盘不参与测速"
    mount = mount_for(devname)
    if not mount:
        return False, "%s 上没有可写的文件系统，无法测写" % devname
    SPEED_ABORT.clear()
    SPEED_PID["p"] = None
    _speed_push(running=True, dev=dev, devname=devname, mount=mount,
                stage="准备中", pct=0.0, cur=None, results=[], err="", elapsed=0.0,
                log="/var/log/speed-%s.log" % devname, direct=True)
    threading.Thread(target=speed_worker, args=(dev, devname, mount,
                                                "/var/log/speed-%s.log" % devname),
                     daemon=True).start()
    SNAP.set(msg="开始测速 %s（顺序 1G + 小文件 + 零碎 + 4K 随机）" % dev, msg_time=time.time())
    return True, "开始测速"


def stop_speed():
    if not SPEED_STATE.get("running"):
        return False, "当前没有测速在跑"
    SPEED_ABORT.set()
    p = SPEED_PID.get("p")
    if p is not None:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(p.pid, signal.SIGTERM)
            except OSError:
                pass
    return True, "已停止测速"


def read_rules():
    """从 renhua 源码里取出 RULES（不 import，避免副作用）。"""
    rules = []
    code = ""
    for path in RENHUA_SRC_CANDIDATES:
        try:
            code = open(path, "r", errors="replace").read()
            if "RULES = [" in code:
                break
        except OSError:
            code = ""
    if not code:
        print("renhua 源码找不到，报错翻译页将无规则", file=sys.stderr)
        return rules
    try:
        ns = {}
        start = code.index("RULES = [")
        end = code.index("SEV_ORDER")
        exec(code[start:end], ns)  # noqa: S102
        rules = ns.get("RULES", [])
    except Exception as ex:
        print("解析 RULES 失败: %s" % type(ex).__name__, file=sys.stderr)
    return rules


TS_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
TS_DMESG = re.compile(r"^\[[A-Za-z]{3} ([A-Za-z]{3} +\d+ \d{2}:\d{2}:\d{2} \d{4})\]")
_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
           "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def _parse_ts(s):
    m = TS_ISO.match(s)
    if m:
        try:
            return time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return None
    m = TS_DMESG.match(s)
    if m:
        parts = m.group(1).split()
        try:
            mon = _MONTHS.get(parts[0][:3], 0)
            day = int(parts[1])
            hh, mm, ss = [int(x) for x in parts[2].split(":")]
            year = int(parts[3])
            if mon:
                return time.mktime((year, mon, day, hh, mm, ss, 0, 0, -1))
        except (ValueError, IndexError):
            return None
    return None


def collect_hits(rules, hours=24):
    """扫 journal + dmesg，返回按 (等级, 次数) 排序的命中列表，带最近/最早出现时间。"""
    if not rules:
        return []
    text = sh("journalctl --since '-%dh' --output=short-iso --no-pager -q 2>/dev/null | tail -n 20000" % hours, 40)
    text += "\n" + sh("dmesg -T 2>/dev/null", 25)
    counts, sample, last, first = {}, {}, {}, {}
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if "COMMAND=" in s and "sudo[" in s:
            continue  # 跳过 sudo 回显：否则我们自己跑的命令名（badblocks 等）会被当成告警
        ts = _parse_ts(s)
        for regex, title, sev, human, advice, cat in rules:
            try:
                if re.search(regex, s, re.IGNORECASE):
                    counts[title] = counts.get(title, 0) + 1
                    sample.setdefault(title, s[:240])
                    if ts:
                        last[title] = max(last.get(title, 0), ts)
                        first[title] = min(first.get(title, ts), ts)
                    break
            except re.error:
                continue
    info = {t[1]: (t[2], t[3], t[4], t[5]) for t in rules}
    order = {"严重": 0, "警告": 1, "提示": 2}
    items = []
    for title, n in counts.items():
        sev, human, advice, cat = info.get(title, ("提示", "", "", ""))
        items.append({"title": title, "sev": sev, "human": human, "advice": advice,
                      "cat": cat, "n": n, "sample": sample.get(title, ""),
                      "last_ts": int(last.get(title, 0)), "first_ts": int(first.get(title, 0))})
    items.sort(key=lambda d: (order.get(d["sev"], 3), -d["n"]))
    return items


class Snapshot(object):
    """采集线程写入的只读快照。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}

    def set(self, **kw):
        with self.lock:
            self.data.update(kw)

    def get(self):
        with self.lock:
            return dict(self.data)


SNAP = Snapshot()
SMART_CACHE = {}


def merge_smart(rows):
    """用 smartctl 的完整型号/序列号覆盖 sysfs（sysfs 的 model 常只有 16 字符）。"""
    for d in rows:
        sv = SMART_CACHE.get(d["name"])
        if not sv:
            if d.get("model", "").startswith("ATA "):
                d["model"] = d["model"][4:]
            continue
        if sv.get("_model"):
            d["model"] = sv["_model"]
        elif d.get("model", "").startswith("ATA "):
            d["model"] = d["model"][4:]
        if sv.get("_sn"):
            d["sn"] = sv["_sn"]
    return rows


def collector_loop():
    rules = read_rules()
    t_disk, t_smart, t_hits = 0, 0, 0
    last_sectors, last_time = None, time.time()
    samples = []
    running_pre = False

    while True:
        now = time.time()

        # 1) 盘与链路：2 秒
        if now - t_disk > 2:
            t_disk = now
            rows = merge_smart(disk_rows())
            SNAP.set(disks=rows, links=link_speeds(), cands=scan_candidates(rows),
                     verified=load_verified())

        # 2) 扫描进度：1 秒
        st = scan_state()
        cur = None
        devname = (st.get("dev") or "").split("/")[-1]
        try:
            for line in open("/proc/diskstats", "r"):
                p = line.split()
                if len(p) > 5 and p[2] == devname:
                    cur = int(p[5])
                    break
        except OSError:
            pass
        mbps = None
        if last_sectors is not None and cur is not None and now > last_time:
            mbps = max(0, cur - last_sectors) * 512 / (now - last_time) / 1e6
            samples.append(mbps)
            if len(samples) > 5:
                samples.pop(0)
            mbps = sum(samples) / len(samples) if samples else None
        last_sectors, last_time = cur, now
        st["mbps"] = mbps if st["running"] else 0.0
        SNAP.set(scan=st)

        # 扫描刚结束 → 判定结果（通过则清告警，有问题出建议）
        if running_pre and not st["running"] and SCAN_PROC.get("dev"):
            finish_scan(SCAN_PROC["dev"])
            running_pre = False
        running_pre = st["running"]

        # 2b) 测速状态由测速线程自己推送（SNAP.speed），这里不再轮询

        # 3) SMART：60 秒（逐盘）
        if now - t_smart > 60:
            t_smart = now
            rows = SNAP.get().get("disks", [])
            names = [d["name"] for d in rows]
            for n in names:
                if n not in SMART_CACHE:
                    SMART_CACHE[n] = smart_read(n)
            for k in list(SMART_CACHE):
                if k not in names:
                    SMART_CACHE.pop(k, None)
            SNAP.set(disks=merge_smart(rows), smart=dict(SMART_CACHE))

        # 4) 报错命中：30 秒（已校验通过的盘的历史告警自动消掉）
        if now - t_hits > 30:
            t_hits = now
            hits = collect_hits(rules)
            ver = load_verified()
            SNAP.set(hits=annotate_hits(hits, ver),
                     verified=ver, cover_ts=cover_ts_of(ver), hits_time=time.time())

        time.sleep(1)


# =====================  键盘（evdev + 独占） =====================
KEY_F1, KEY_F5, KEY_F6, KEY_F10, KEY_ESC = 59, 63, 64, 68, 1
KEY_LEFT, KEY_RIGHT = 105, 106
KEY_UP, KEY_DOWN = 103, 108
KEY_ENTER, KEY_KPENTER = 28, 96
KEY_1, KEY_5 = 2, 6
EV_KEY = 1
EVIOCGRAB = 0x40044590

KEYQ = queue.SimpleQueue()


def pick_keyboards():
    """所有可当键盘用的 evdev 设备（名称含 Keyboard，排除 Consumer/System Control 与 Video Bus）。"""
    found = []
    for path in sorted(glob.glob("/dev/input/event*"), key=lambda s: int(re.sub(r"\D", "", s) or 0)):
        base = os.path.basename(path)
        try:
            name = open("/sys/class/input/%s/device/name" % base).read().strip()
        except OSError:
            continue
        if "Consumer" in name or "System Control" in name or "Video Bus" in name:
            continue
        caps = ""
        try:
            caps = open("/sys/class/input/%s/device/capabilities/key" % base).read()
        except OSError:
            pass
        if "Keyboard" in name or ("kbd" in caps and len(caps.split()) > 2):
            found.append((path, name))
    return found


def keyboard_worker(path, name):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    grab = os.environ.get("RENHUA_NO_GRAB", "") != "1"
    if grab:
        try:
            import fcntl
            fcntl.ioctl(fd, EVIOCGRAB, 1)
            print("grabbed %s (%s)" % (path, name), file=sys.stderr)
        except Exception as ex:
            print("grab failed on %s (%s), passive" % (path, type(ex).__name__), file=sys.stderr)
    else:
        print("passive %s (%s)" % (path, name), file=sys.stderr)

    fmt = "llHHi"
    sz = struct.calcsize(fmt)
    while True:
        try:
            data = os.read(fd, sz * 32)
        except OSError:
            time.sleep(1)
            continue
        for i in range(0, len(data) - sz + 1, sz):
            _s, _u, typ, code, val = struct.unpack_from(fmt, data, i)
            if typ != EV_KEY or val != 1:
                continue
            print("key code=%d dev=%s" % (code, path), file=sys.stderr)
            if KEY_F1 <= code <= KEY_F6:
                KEYQ.put(("page", code - KEY_F1))
            elif code in (KEY_ESC, KEY_F10):
                KEYQ.put(("quit", 0))
            elif code in (KEY_LEFT, KEY_UP):
                KEYQ.put(("axis", -1))
            elif code in (KEY_RIGHT, KEY_DOWN):
                KEYQ.put(("axis", 1))
            elif code in (KEY_ENTER, KEY_KPENTER):
                KEYQ.put(("enter", 0))
            elif code == KEY_1:
                KEYQ.put(("page", 0))
            elif code == KEY_2:
                KEYQ.put(("page", 1))
            elif code == KEY_3:
                KEYQ.put(("page", 2))
            elif code == KEY_4:
                KEYQ.put(("page", 3))
            elif code == KEY_5:
                KEYQ.put(("page", 4))


def keyboard_loop():
    """持续发现新接入的键盘（热插拔/虚拟设备），每个设备一个读线程。"""
    seen = set()
    while True:
        try:
            for path, name in pick_keyboards():
                if path in seen:
                    continue
                seen.add(path)
                print("attach keyboard: %s (%s)" % (path, name), file=sys.stderr)
                threading.Thread(target=keyboard_worker, args=(path, name), daemon=True).start()
            for p in list(seen):
                if not os.path.exists(p):
                    seen.discard(p)
        except Exception as ex:
            print("keyboard scan error: %s" % type(ex).__name__, file=sys.stderr)
        time.sleep(1)


# =====================  绘制工具  =====================
def font(size, bold=False):
    f = QFont(FONT_FAMILY)
    f.setPixelSize(size)
    f.setBold(bold)
    return f


def draw_text(p, x, y, text, size, color=C_TEXT, bold=False, align="left", wbox=None):
    """x 为矩形左边界，wbox 为矩形宽度（对齐都相对这个矩形）。"""
    p.setFont(font(size, bold))
    p.setPen(QPen(color))
    w = wbox if wbox is not None else (W - x - 20)
    r = QRect(x, y, w, size + 14)
    flags = {"left": Qt.AlignLeft, "right": Qt.AlignRight, "center": Qt.AlignHCenter}[align] | Qt.AlignTop
    p.drawText(r, flags, text)


def elide(p, text, width):
    """单行超宽时尾部省略，保证行式布局不串行。"""
    if not text:
        return ""
    fm = QFontMetrics(p.font())
    return fm.elidedText(text, Qt.ElideRight, max(20, width))


def card(p, x, y, w, h, fill=C_PANEL, border=C_LINE, radius=10):
    p.setPen(QPen(border, 1))
    p.setBrush(QBrush(fill))
    p.drawRoundedRect(QRect(x, y, w, h), radius, radius)


def pill(p, x, y, text, size=17, fill=C_PANEL, fg=C_TEXT, border=C_LINE, padx=14, h=34):
    p.setFont(font(size))
    fm = p.fontMetrics()
    tw = fm.horizontalAdvance(text)
    p.setPen(QPen(border, 1))
    p.setBrush(QBrush(fill))
    p.drawRoundedRect(QRect(x, y, tw + padx * 2, h), h // 2, h // 2)
    p.setPen(QPen(fg))
    p.drawText(QRect(x + padx, y, tw + 4, h), Qt.AlignLeft | Qt.AlignVCenter, text)
    return tw + padx * 2


def pill_r(p, right_x, y, text, size=17, fill=C_PANEL, fg=C_TEXT, border=C_LINE, padx=14, h=34):
    """右端对齐的 pill：right_x 为胶囊右边界。"""
    p.setFont(font(size))
    tw = p.fontMetrics().horizontalAdvance(text)
    w = tw + padx * 2
    return pill(p, right_x - w, y, text, size, fill, fg, border, padx, h)


def sev_color(sev):
    return {"严重": C_RED, "警告": C_ORANGE, "提示": C_BLUE}.get(sev, C_DIM)


def fmt_gb(v):
    if not v:
        return "--"
    if v >= 1000:
        return "%.0f" % v
    return "%.1f" % v


def fmt_spd(s):
    if not s:
        return "无盘"
    return s.replace(" Gbps", "G").replace("0 Gbps", "G")


# =====================  主窗口  =====================
class Console(QWidget):
    def __init__(self, page=4):
        super().__init__()
        self.page = page      # 默认 F5 报错翻译（总览）
        self.sel = 0          # F2 页选中的盘下标
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.resize(FBW, FBH)
        self.buf = None       # 离屏整帧缓冲，避免逐元素直写 framebuffer 时闪
        self.setCursor(Qt.BlankCursor)
        self.tick = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.on_tick)
        self.timer.start(1000)
        self.keytimer = QTimer(self)
        self.keytimer.timeout.connect(self.poll_keys)
        self.keytimer.start(60)
        self.keeptimer = QTimer(self)
        self.keeptimer.timeout.connect(self.keep_awake)
        self.keeptimer.start(10000)

    # ---- 键盘 ----
    def poll_keys(self):
        changed = False
        cands = SNAP.get().get("cands", [])
        while True:
            try:
                kind, val = KEYQ.get_nowait()
            except queue.Empty:
                break
            if kind == "quit":
                if self.page == 1 and SNAP.get().get("scan", {}).get("running"):
                    stop_scan()          # 扫描中先停扫描，按第二次才退出
                elif self.page == 5 and SNAP.get().get("speed", {}).get("running"):
                    stop_speed()
                else:
                    QApplication.quit()
            elif kind == "page":
                self.page = val
                PAGE_NOW["n"] = val
                changed = True
            elif kind == "axis":
                if self.page in (1, 5) and cands:
                    self.sel = max(0, min(len(cands) - 1, self.sel + val))
                else:
                    self.page = (self.page + (1 if val > 0 else -1)) % len(PAGES)
                changed = True
            elif kind == "enter":
                if self.page == 1:
                    self.toggle_scan(cands)
                elif self.page == 5:
                    self.toggle_speed(cands)
                changed = True
            elif kind == "next":
                self.page = (self.page + 1) % len(PAGES)
                changed = True
            elif kind == "prev":
                self.page = (self.page - 1) % len(PAGES)
                changed = True
        if changed:
            self.update()

    def toggle_scan(self, cands):
        """F2 页：Enter 开始/停止扫描。"""
        scan = SNAP.get().get("scan", {})
        if scan.get("running"):
            stop_scan()
            return
        if not cands:
            SNAP.set(msg="没有可扫描的盘（系统盘不参与）", msg_time=time.time())
            return
        d = cands[max(0, min(self.sel, len(cands) - 1))]
        start_scan("/dev/" + d["name"])

    def toggle_speed(self, cands):
        """F6 页：Enter 开始/停止测速。"""
        if SNAP.get().get("speed", {}).get("running"):
            stop_speed()
            return
        if not cands:
            SNAP.set(msg="没有可测速的盘（系统盘不参与）", msg_time=time.time())
            return
        d = cands[max(0, min(self.sel, len(cands) - 1))]
        start_speed("/dev/" + d["name"])

    def keep_awake(self):
        for path in ("/sys/class/graphics/fb0/blank",):
            try:
                with open(path, "w") as f:
                    f.write("0")
            except OSError:
                pass
        for c in ("card0", "card1"):
            for dp in ("DP-1", "DP-2", "HDMI-A-1"):
                try:
                    with open("/sys/class/drm/%s-%s/dpms" % (c, dp), "w") as f:
                        f.write("On")
                except OSError:
                    pass

    def on_tick(self):
        self.tick += 1
        self.update()

    # ---- 画 ----
    def paintEvent(self, _ev):
        # 先画到离屏缓冲，再整帧一次性贴到 framebuffer：避免 linuxfb 上“清屏→逐个画”的中间态被看到（闪）
        if self.buf is None:
            self.buf = QImage(W, H, QImage.Format_RGB32)
        p = QPainter(self.buf)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(0, 0, W, H, C_BG)
        snap = SNAP.get()

        self.draw_header(p, snap)
        if self.page == 0:
            self.page_help(p, snap)
        elif self.page == 1:
            self.page_scan(p, snap)
        elif self.page == 2:
            self.page_ports(p, snap)
        elif self.page == 3:
            self.page_smart(p, snap)
        elif self.page == 4:
            self.page_errors(p, snap)
        else:
            self.page_speed(p, snap)
        self.draw_hotkeys(p)
        p.end()

        q = QPainter(self)
        q.setRenderHint(QPainter.SmoothPixmapTransform, True)
        q.drawImage(QRect(0, 0, FBW, FBH), self.buf, QRect(0, 0, W, H))
        q.end()

    def draw_header(self, p, snap):
        g = QLinearGradient(0, 0, 0, 96)
        g.setColorAt(0, QColor(20, 24, 32))
        g.setColorAt(1, QColor(13, 15, 19))
        p.fillRect(0, 0, W, 96, QBrush(g))
        p.setPen(QPen(C_LINE, 1))
        p.drawLine(0, 96, W, 96)

        draw_text(p, 50, 22, "存储体检控制台", 40, C_TEXT, True)
        tag = PAGES[self.page]
        draw_text(p, 470, 40, "%s · %s" % (tag[0], tag[1]), 24, C_BLUE)

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        disks = snap.get("disks", [])
        right = "%s   %s   %d 块盘" % (HOST_TAG, now, len(disks))
        draw_text(p, 50, 46, right, 20, C_DIM, align="right", wbox=W - 100)

    def draw_hotkeys(self, p):
        y = 1006
        p.fillRect(0, y, W, H - y, QColor(16, 18, 24))
        p.setPen(QPen(C_LINE, 1))
        p.drawLine(0, y, W, y)
        x = 50
        for i, (k, name, en) in enumerate(PAGES):
            active = (i == self.page)
            label = "%s %s" % (k, name)
            p.setFont(font(22, active))
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(label)
            if active:
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(QColor(32, 52, 96)))
                p.drawRoundedRect(QRect(x - 12, y + 12, tw + 40, 44), 8, 8)
                p.setPen(QPen(C_TEXT))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(QRect(x - 12, y + 12, tw + 40, 44), 8, 8)
            p.setPen(QPen(C_TEXT if active else C_DIM, 1))
            p.drawText(QRect(x, y + 12, tw + 10, 44), Qt.AlignLeft | Qt.AlignVCenter, label)
            x += tw + 76
        draw_text(p, 40, y + 18, "要被管理就走 SSH；F1-F6 切页，ESC 退出", 18, C_DIM2,
                  align="right", wbox=W - 80)

    # ---------- F1 帮助 ----------
    def page_help(self, p, snap):
        card(p, 50, 120, 880, 450)
        draw_text(p, 80, 140, "热键（MHDD 风格分页）", 28, C_TEXT, True)
        rows = [
            ("F1", "帮助", "本页：热键与数据来源说明"),
            ("F2", "坏块扫描", "选盘（← →）后按 Enter 开始/停止 badblocks 读校验"),
            ("F3", "端口识别", "ATA1-6 各口是否有盘、型号、容量、链路速率"),
            ("F4", "健康体检", "SMART 关键属性：重映射/待映射/离线不可纠正/CRC/温度"),
            ("F5", "报错翻译", "近 24 小时内核报错命中 renhua 规则后的人话解释"),
            ("F6", "测速", "选盘后按 Enter 跑 512MB 顺序读，测链路与盘的真实读速"),
        ]
        y = 196
        for k, n, d in rows:
            pill(p, 80, y, k, 20, fill=QColor(32, 52, 96), fg=C_TEXT, border=C_BLUE, h=38)
            draw_text(p, 160, y + 4, n, 22, C_TEXT, True)
            draw_text(p, 330, y + 8, d, 19, C_DIM)
            y += 58

        card(p, 950, 120, 920, 450)
        draw_text(p, 980, 140, "按键与退出", 28, C_TEXT, True)
        tips = [
            "F1-F6 直接切页；F2/F6 页内 ← → 选盘、Enter 开始/停止，其它页 ← → 翻页",
            "ESC / F10 退出；若正在扫描，第一次按 ESC 只停扫描，再按才退出",
            "键盘由本程序独占（EVIOCGRAB），所以 F1-F5 不会触发控制台切 VT",
            "代价：程序运行期间本机控制台键盘不可用，请用 SSH 管理",
            "临时放弃独占：RENHUA_NO_GRAB=1 重启服务（此时按 F 键会闪一下 VT）",
            "停用整套屏显：systemctl stop renhua-console",
        ]
        y = 196
        for t in tips:
            p.setPen(QPen(C_BLUE, 2))
            p.setBrush(QBrush(C_BLUE))
            p.drawEllipse(QPointF(994, y + 12), 4, 4)
            draw_text(p, 1010, y, t, 19, C_DIM)
            y += 58

        # 硬件概况
        disks = snap.get("disks", [])
        card(p, 50, 600, 1820, 370)
        draw_text(p, 80, 618, "当前硬件概况", 26, C_TEXT, True)
        links = snap.get("links", {})
        y = 672
        for d in disks:
            sev, col = smart_verdict((snap.get("smart") or {}).get(d["name"]))
            name = "ATA%d" % d["ata"] if d["ata"] else d["name"]
            p.setPen(QPen(col, 2))
            p.setBrush(QBrush(col))
            p.drawEllipse(QPointF(100, y + 16), 8, 8)
            draw_text(p, 126, y, "%-6s %s" % (name, d["model"]), 22, C_TEXT, True)
            draw_text(p, 900, y + 4, "%s GB" % fmt_gb(d["size_gb"]), 20, C_DIM)
            draw_text(p, 1100, y + 4, fmt_spd(d["spd"]), 20,
                      C_GREEN if d["spd"].startswith("6.0") else C_ORANGE)
            draw_text(p, 1300, y + 4, sev, 20, col)
            draw_text(p, 1500, y + 4, "SN %s" % (d["sn"] or "--"), 18, C_DIM2)
            y += 62
        used = [d for d in disks if d["ata"]]
        empties = [i for i in range(1, 7) if i not in [d["ata"] for d in used]]
        draw_text(p, 80, y + 10, "空口：%s" % ("、".join("ATA%d" % i for i in empties) or "无"),
                  20, C_DIM)
        draw_text(p, 80, y + 56, "数据来源：/sys/block、/sys/class/ata_link、smartctl -A、"
                                 "journalctl(24h)、%s" % ATALOG, 18, C_DIM2)

    # ---------- F2 坏块扫描 ----------
    def page_scan(self, p, snap):
        st = snap.get("scan", {})
        cands = snap.get("cands", [])
        running = st.get("running")
        pct = st.get("pct")
        devname = (st.get("dev") or "").split("/")[-1]
        if cands:
            self.sel = max(0, min(self.sel, len(cands) - 1))

        # 标题 + 状态
        draw_text(p, 50, 118, "坏块扫描", 30, C_TEXT, True)
        draw_text(p, 200, 128, "badblocks 读校验 · 只读不写 · 坏块只记录不屏蔽", 18, C_DIM)
        if running:
            state_txt, state_col = "扫描进行中", C_GREEN
        elif pct is not None:
            state_txt, state_col = "上次扫描已停止", C_ORANGE
        else:
            state_txt, state_col = "待选择磁盘", C_DIM
        pill_r(p, 1870, 118, state_txt, 20, fill=QColor(28, 34, 44), fg=state_col,
               border=state_col, h=40)

        # 盘选择区：每盘一张卡，选中高亮
        y = 176
        self.draw_pick(p, snap, y, "方向键选择磁盘，Enter 开始扫描；扫描中再按 Enter 停止")

        # 任务行：扫描中显示在跑的盘，否则显示选中的盘（Enter 就将扫它）
        target = running and devname or ""
        if not target and cands:
            target = cands[self.sel]["name"]
        d = next((x for x in snap.get("disks", []) if x["name"] == target), None)
        if d:
            head = "%s · %s（%s GB，%s）%s" % (
                target, d["model"], fmt_gb(d["size_gb"]), fmt_spd(d["spd"]),
                "" if running else "  ← 即将扫描这块")
        else:
            head = target or "未选择磁盘"
        if target and target in (snap.get("verified") or {}):
            head += "　[已校验通过]"
        draw_text(p, 50, 328, head, 22, C_TEXT, True)
        if running:
            draw_text(p, 1870 - 320, 328, "扫描中，按 Enter 停止", 18, C_GREEN, align="right", wbox=320)
        else:
            draw_text(p, 1870 - 320, 328, "按 Enter 开始扫描", 18, C_BLUE, align="right", wbox=320)

        # 进度条
        bar = QRect(50, 366, 1820, 70)
        p.setPen(QPen(C_LINE, 1))
        p.setBrush(QBrush(QColor(24, 28, 36)))
        p.drawRoundedRect(bar, 6, 6)
        if pct is not None:
            fillw = int(bar.width() * max(0.0, min(100.0, pct)) / 100.0)
            g = QLinearGradient(bar.x(), 0, bar.x() + max(fillw, 1), 0)
            g.setColorAt(0, QColor(58, 190, 130))
            g.setColorAt(1, C_GREEN)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(g))
            p.drawRoundedRect(QRect(bar.x(), bar.y(), max(fillw, 8), bar.height()), 6, 6)

        # 百分比 / 速度
        big = "%.1f%%" % pct if pct is not None else "0.0%"
        draw_text(p, 50, 450, big, 84, C_TEXT, True, align="right", wbox=W - 100)
        spd = st.get("mbps")
        if not running or spd is None:
            spd_txt = "无活动"
        elif spd >= 1:
            spd_txt = "%.1f MB/s" % spd
        else:
            spd_txt = "%.0f KB/s" % (spd * 1000)
        draw_text(p, 80, 448, "读取速度", 17, C_DIM)
        draw_text(p, 80, 474, spd_txt, 34, C_BLUE if running else C_DIM2, True)

        # 三块数据
        blocks = [
            ("已用时间", self.hms(st.get("elapsed"))),
            ("剩余时间（估算）", self.eta(st.get("elapsed"), pct) if running else "----"),
            ("错误数 读/写/校验", st.get("errs") or "--"),
        ]
        x = 50
        for i, (k, v) in enumerate(blocks):
            card(p, x, 570, 580, 110)
            draw_text(p, x + 26, 584, k, 19, C_DIM)
            draw_text(p, x + 26, 616, v, 36, C_TEXT, True)
            x += 620

        # 说明
        card(p, 50, 700, 1820, 140, fill=C_PANEL2)
        draw_text(p, 76, 714, "扫描说明", 22, C_TEXT, True)
        logpath = st.get("log") or ("/var/log/bb-%s.log" % (target or "sdb"))
        draw_text(p, 76, 752, "命令：badblocks -sv -b 4096 /dev/%s　输出：%s" % (target or "sdb", logpath),
                  18, C_DIM)
        draw_text(p, 76, 786, "只读校验，不写盘、不改数据；扫完只列出坏块位置，是否屏蔽由你决定；"
                              "扫描期间系统可正常用（低优先级 IO）。", 18, C_DIM)
        advice = snap.get("scan_advice")
        if advice:
            # 扫出坏块 → 直接给处置建议（盖掉说明文字）
            p.setPen(QPen(C_RED, 2))
            p.setBrush(QBrush(QColor(40, 24, 26)))
            p.drawRoundedRect(QRect(50, 700, 1820, 140), 10, 10)
            draw_text(p, 76, 714, "处置建议", 22, C_RED, True)
            draw_text(p, 76, 754, advice, 19, C_TEXT)
            draw_text(p, 76, 790, "扫完 0 坏块会自动清掉该盘的历史告警；按 Enter 可重扫或换盘再扫。", 17, C_DIM)
        elif not running and pct is None:
            draw_text(p, 76, 818, "尚未开始：选好盘后按 Enter。", 18, C_DIM)
        else:
            msg = snap.get("msg")
            if msg:
                color = C_GREEN if ("已开始" in msg or "校验通过" in msg) else C_ORANGE
                draw_text(p, 76, 818, msg, 18, color)

        # ATA 灯条
        self.draw_ata_bar(p, snap, y0=858)

    def draw_pick(self, p, snap, y, hint):
        """盘选择卡（F2 / F6 共用）。"""
        cands = snap.get("cands", [])
        if cands:
            self.sel = max(0, min(self.sel, len(cands) - 1))
        card(p, 50, y, 1820, 132, fill=C_PANEL2)
        draw_text(p, 76, y + 12, hint, 18, C_DIM)
        if not cands:
            draw_text(p, 76, y + 62, "没有可选的盘（系统盘不参与）", 22, C_ORANGE)
            return
        cw = min(560, (1700 - (len(cands) - 1) * 20) // max(len(cands), 1))
        for i, d in enumerate(cands[:3]):
            cx = 76 + i * (cw + 20)
            active = (i == self.sel)
            p.setPen(QPen(C_BLUE if active else C_LINE, 3 if active else 1))
            p.setBrush(QBrush(QColor(26, 34, 50) if active else QColor(22, 26, 34)))
            p.drawRoundedRect(QRect(cx, y + 48, cw, 76), 8, 8)
            name = "ATA%d" % d["ata"] if d["ata"] else d["name"]
            draw_text(p, cx + 18, y + 60, "%s  %s" % (name, d["model"][:26]), 20,
                      C_TEXT if active else C_DIM, active)
            draw_text(p, cx + 18, y + 94, "%s GB   %s" % (fmt_gb(d["size_gb"]), fmt_spd(d["spd"])),
                      17, C_DIM2)
            if active:
                draw_text(p, cx + cw - 90, y + 60, "已选中", 18, C_BLUE, True)

    # ---------- F6 测速 ----------
    def page_speed(self, p, snap):
        st = snap.get("speed", {})
        cands = snap.get("cands", [])
        running = st.get("running")
        pct = st.get("pct") or 0.0
        devname = st.get("devname") or (st.get("dev") or "").split("/")[-1]
        results = st.get("results") or []

        draw_text(p, 50, 118, "磁盘测速", 30, C_TEXT, True)
        draw_text(p, 200, 128, "一轮完整测试：顺序读/写 1GB · 小文件 32MB · 零碎 32MB · RND4K Q1T1", 18, C_DIM)
        if running:
            state_txt, state_col = "测速进行中 %d/%d" % (st.get("stage_i", 0) + 1, len(STAGES)), C_GREEN
        elif st.get("stage") == "已停止":
            state_txt, state_col = "上次测速已停止", C_ORANGE
        elif st.get("err"):
            state_txt, state_col = "上次测速出错", C_RED
        elif results:
            state_txt, state_col = "上次测速已完成", C_ORANGE
        else:
            state_txt, state_col = "待选择磁盘", C_DIM
        pill_r(p, 1870, 118, state_txt, 20, fill=QColor(28, 34, 44), fg=state_col,
               border=state_col, h=40)

        self.draw_pick(p, snap, 176, "方向键选择磁盘，Enter 开始测速；测速中再按 Enter 停止")

        target = devname or (cands[self.sel]["name"] if cands else "")
        d = next((x for x in snap.get("disks", []) if x["name"] == target), None)
        if d:
            head = "%s · %s（%s GB，%s）%s" % (
                target, d["model"], fmt_gb(d["size_gb"]), fmt_spd(d["spd"]),
                "" if running else "  ← 即将测速这块")
        else:
            head = target or "未选择磁盘"
        if st.get("mount"):
            head += "　挂载 %s" % st["mount"]
        draw_text(p, 50, 326, head, 21, C_TEXT, True)
        hint = "按 Enter 停止" if running else "按 Enter 开始测速"
        draw_text(p, 1870 - 320, 326, hint, 18, C_GREEN if running else C_BLUE,
                  align="right", wbox=320)

        # 总进度
        bar = QRect(50, 360, 1820, 60)
        p.setPen(QPen(C_LINE, 1))
        p.setBrush(QBrush(QColor(24, 28, 36)))
        p.drawRoundedRect(bar, 6, 6)
        if pct:
            fillw = int(bar.width() * max(0.0, min(100.0, pct)) / 100.0)
            g = QLinearGradient(bar.x(), 0, bar.x() + max(fillw, 1), 0)
            g.setColorAt(0, QColor(58, 190, 130))
            g.setColorAt(1, C_GREEN)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(g))
            p.drawRoundedRect(QRect(bar.x(), bar.y(), max(fillw, 8), bar.height()), 6, 6)

        # 当前阶段 + 实时速度
        stage = st.get("stage") or "空闲"
        draw_text(p, 50, 432, stage, 26, C_TEXT, True)
        cur = st.get("cur")
        cur_txt = "%.1f MB/s" % cur if cur else "--"
        draw_text(p, 50, 470, cur_txt, 66, C_BLUE if running else C_DIM2, True,
                  align="right", wbox=W - 100)
        draw_text(p, 80, 470, "总进度 %.0f%%　用时 %s" % (pct, self.hms(st.get("elapsed"))), 20, C_DIM)

        # 结果表（8 项，两列）
        card(p, 50, 558, 1820, 218, fill=C_PANEL)
        draw_text(p, 76, 570, "本次结果", 20, C_TEXT, True)
        rows = list(results)
        for i, (nm, val) in enumerate(rows[:8]):
            col = i % 2
            row = i // 2
            cx = 76 + col * 900
            cy = 600 + row * 42
            draw_text(p, cx, cy, nm, 19, C_DIM)
            draw_text(p, cx + 340, cy, val, 19, C_TEXT, True, wbox=400)
        if not rows:
            draw_text(p, 76, 600, "还没跑过：选盘后按 Enter，8 个阶段依次跑完（含 1GB 临时文件，结束自动删除）。",
                      18, C_DIM)

        # 说明
        card(p, 50, 790, 1820, 150, fill=C_PANEL2)
        draw_text(p, 76, 802, "测速说明", 20, C_TEXT, True)
        cmd_txt = st.get("log") or ""
        draw_text(p, 76, 836, "顺序读写用 dd（1M×1024，读 iflag=direct 绕缓存；写落在 %s）　小文件/零碎读也走 O_DIRECT" % (
            st.get("mount") or "该盘挂载目录"), 18, C_DIM)
        draw_text(p, 76, 866, "小文件=32×1MB（逐文件 fsync）　零碎=8192×4KB　RND4K=4K 随机 2000 次、队列深度 1、单线程",
                  18, C_DIM)
        draw_text(p, 76, 896, "测试数据写在 %s，跑完/停止都会删除；日志 %s" % (
            st.get("dir") or "<挂载点>/.renhua-speed", cmd_txt), 17, C_DIM2)
        if st.get("err"):
            draw_text(p, 1300, 836, "错误：%s" % st["err"], 17, C_RED)

    def hms(self, sec):
        if sec is None:
            return "--:--:--"
        sec = int(sec)
        return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)

    def eta(self, elapsed, pct):
        if elapsed is None or pct is None or pct < 0.5:
            return "----"
        return self.hms(elapsed * (100.0 - pct) / pct)

    def draw_ata_bar(self, p, snap, y0=860):
        y = y0
        card(p, 50, y, 1820, 120, fill=C_PANEL2)
        draw_text(p, 76, y + 14, "接口状态", 20, C_DIM)
        disks = snap.get("disks", [])
        links = snap.get("links", {})
        have = {d["ata"]: d for d in disks if d["ata"]}
        for i in range(6):
            x = 76 + 290 * (i % 6)
            ok = (i + 1) in have
            col = C_GREEN if ok else C_DIM2
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(col))
            p.drawRoundedRect(QRect(x, y + 52, 16, 16), 4, 4)
            draw_text(p, x + 28, y + 46, "ATA%d" % (i + 1), 22, C_TEXT if ok else C_DIM)
            sub = fmt_spd(links.get(i + 1, "")) if ok else "空"
            draw_text(p, x + 28, y + 80, sub, 16, C_DIM2)
        draw_text(p, 76, y + 52, "绿=有盘  灰=空口", 17, C_DIM2, align="right", wbox=W - 152)

    # ---------- F3 端口识别 ----------
    def page_ports(self, p, snap):
        disks = snap.get("disks", [])
        links = snap.get("links", {})
        have = {d["ata"]: d for d in disks if d["ata"]}

        card(p, 50, 120, 1820, 60, fill=C_PANEL2)
        heads = [("端口", 80), ("状态", 260), ("型号 / 序列号", 460), ("容量", 1180),
                 ("链路", 1420), ("温度", 1600)]
        for t, x in heads:
            draw_text(p, x, 138, t, 20, C_DIM, True)

        y = 200
        smart = snap.get("smart") or {}
        for i in range(1, 7):
            d = have.get(i)
            card(p, 50, y, 1820, 100, fill=C_PANEL if d else C_PANEL2)
            draw_text(p, 80, y + 30, "ATA%d" % i, 30, C_TEXT if d else C_DIM2, True)
            if d:
                col = C_GREEN
                draw_text(p, 260, y + 34, "有盘", 22, col)
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(col))
                p.drawRoundedRect(QRect(250, y + 66, 60, 6), 3, 3)
                draw_text(p, 460, y + 20, d["model"], 22, C_TEXT)
                draw_text(p, 460, y + 58, "SN %s   %s" % (d["sn"] or "--", d["name"]), 17, C_DIM2)
                draw_text(p, 1180, y + 34, "%s GB" % fmt_gb(d["size_gb"]), 22, C_TEXT)
                spd = d["spd"]
                spd_col = C_GREEN if spd.startswith("6.0") else (C_ORANGE if spd else C_DIM2)
                draw_text(p, 1420, y + 34, fmt_spd(spd), 22, spd_col, True)
                if not spd.startswith("6.0") and spd:
                    draw_text(p, 1420, y + 66, "降速，查线缆/换口", 15, C_ORANGE)
                sv = smart.get(d["name"])
                t = sv.get("温度") if sv else None
                draw_text(p, 1600, y + 34, "%s °C" % (t if t is not None else "--"),
                          22, C_TEXT if t else C_DIM2)
            else:
                draw_text(p, 260, y + 34, "空口", 22, C_DIM2)
                draw_text(p, 460, y + 34, "--", 22, C_DIM2)
                draw_text(p, 1180, y + 34, "--", 22, C_DIM2)
                draw_text(p, 1420, y + 34, "--", 22, C_DIM2)
                draw_text(p, 1600, y + 34, "--", 22, C_DIM2)
            y += 110

        draw_text(p, 80, y + 16, "链路速率读 /sys/class/ata_link/linkN/sata_spd；低于 6.0 说明协商降速，"
                                 "优先换 SATA 线或换口。", 18, C_DIM2)

    # ---------- F4 健康体检 ----------
    def page_smart(self, p, snap):
        disks = snap.get("disks", [])
        smart = snap.get("smart") or {}
        if not disks:
            draw_text(p, 100, 300, "没有检测到任何磁盘", 40, C_DIM)
            return
        cards = min(len(disks), 3)
        cw = (1820 - (cards - 1) * 40) // cards
        for idx, d in enumerate(disks[:3]):
            x = 50 + idx * (cw + 40)
            v = smart.get(d["name"])
            sev, col = smart_verdict(v)
            card(p, x, 120, cw, 420)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(col))
            p.drawRoundedRect(QRect(x, 120, 8, 420), 4, 4)

            draw_text(p, x + 28, 140, "ATA%d · %s" % (d["ata"] or 0, d["name"]), 18, C_DIM)
            draw_text(p, x + 28, 176, d["model"], 24, C_TEXT, True)
            draw_text(p, x + 28, 216, "SN %s   %s GB" % (d["sn"] or "--", fmt_gb(d["size_gb"])),
                      17, C_DIM2)
            pill(p, x + cw - 150, 140, sev, 18, fill=QColor(28, 34, 44), fg=col, border=col)

            rows = [("温度", "温度", "°C"), ("重映射扇区", "重映射扇区", ""),
                    ("待映射扇区", "待映射扇区", ""), ("离线不可纠正", "离线不可纠正", ""),
                    ("接口 CRC", "接口 CRC", ""), ("通电小时", "通电小时", "h")]
            y = 262
            for label, key, unit in rows:
                val = v.get(key) if v else None
                draw_text(p, x + 28, y, label, 19, C_DIM)
                if val is None:
                    draw_text(p, x + 30, y, "--", 22, C_DIM2, align="right", wbox=cw - 60)
                else:
                    bad = key in ("重映射扇区", "待映射扇区", "离线不可纠正") and val > 0
                    c = C_RED if bad else (C_ORANGE if key == "接口 CRC" and val > 0 else C_TEXT)
                    draw_text(p, x + 30, y, "%d%s" % (val, unit), 22, c, True,
                              align="right", wbox=cw - 60)
                y += 44

        # 结论条
        card(p, 50, 570, 1820, 150, fill=C_PANEL2)
        draw_text(p, 76, 588, "结论", 24, C_TEXT, True)
        lines = []
        for d in disks:
            v = smart.get(d["name"])
            sev, col = smart_verdict(v)
            extra = ""
            if v:
                pm = v.get("待映射扇区", 0)
                ou = v.get("离线不可纠正", 0)
                rc = v.get("重映射扇区", 0)
                if pm or ou:
                    extra = "（已确认坏扇区：待映射 %d / 离线不可纠正 %d）" % (pm, ou)
                if rc:
                    extra += "（已替换 %d 个扇区）" % rc
            lines.append(("ATA%d %s：%s%s" % (d["ata"] or 0, d["model"][:28], sev, extra), col))
        y = 626
        for txt, col in lines:
            p.setPen(QPen(col, 2))
            p.setBrush(QBrush(col))
            p.drawEllipse(QPointF(86, y + 12), 5, 5)
            draw_text(p, 104, y, txt, 20, C_TEXT)
            y += 40

        card(p, 50, 740, 1820, 236, fill=C_PANEL2)
        draw_text(p, 76, 756, "怎么读这些数", 24, C_TEXT, True)
        tips = [
            "重映射/待映射/离线不可纠正：只要有非零，就是盘片上已有读不回来的扇区；数值继续涨=还在恶化。",
            "接口 CRC：是数据线和接口的信号错误计数，不一定是盘坏；先换线换口再看是否继续涨。",
            "通电小时：很低的通电时间配上大量坏扇区，通常说明是长期闲置/运输受损，而不是磨损老化。",
            "SMART 只做参考：坏道扫描（F2）才是逐块实测，两者结合判断是否弃用这块盘。",
        ]
        y = 800
        for t in tips:
            draw_text(p, 90, y, "· " + t, 18, C_DIM)
            y += 40

    # ---------- F5 报错翻译 ----------
    def page_errors(self, p, snap):
        hits = snap.get("hits")
        if hits is None:
            draw_text(p, 100, 300, "正在扫描 journal / dmesg …", 34, C_DIM)
            return
        t = snap.get("hits_time")
        sub = "近 24 小时 · 每 30 秒重扫 journal 与 dmesg · 命中 %d 类" % len(hits) if hits else \
              "近 24 小时 · 每 30 秒重扫 journal 与 dmesg"
        draw_text(p, 50, 120, sub + ("（更新于 %s）" % time.strftime("%H:%M:%S", time.localtime(t))
                                    if t else ""), 19, C_DIM)

        if not hits:
            card(p, 50, 170, 1820, 380)
            draw_text(p, 100, 220, "近 24 小时没有活跃报错", 52, C_GREEN, True)
            draw_text(p, 100, 300, "journal 与 dmesg 中没有任何一行命中 renhua 的内置规则。", 22, C_DIM)
            draw_text(p, 100, 340, "历史上出现过的类别会在这里显示；超过 24 小时不再出现的会自动撤下，"
                                   "所以这张屏永远只反映“现在真正还在报的问题”。", 20, C_DIM)
            draw_text(p, 100, 420, "命令行对照：sudo dmesg | renhua　　renhua --top　　renhua --rules",
                      19, C_DIM2)
            card(p, 50, 580, 1820, 180, fill=C_PANEL2)
            draw_text(p, 76, 600, "规则覆盖范围（26 类）", 24, C_TEXT, True)
            draw_text(p, 76, 644, "硬盘物理层：UNC 不可纠正、介质错误、I/O 失败　│　链路：SATA 掉线、物理层错误、"
                                  "反复复位、命令失败", 18, C_DIM)
            draw_text(p, 76, 680, "文件系统：btrfs 事务/校验和/scrub　│　ext4 错误与只读　│　阵列：降级、损坏、重建　│　"
                                  "系统：OOM、满盘、hung task、panic", 18, C_DIM)
            draw_text(p, 76, 720, "按严重 → 警告 → 提示排序，同类合并计数；原文只在命令行 renhua 里给。",
                      18, C_DIM2)
            return

        shown = hits[:8]
        y = 168
        rowh = 90
        for i, hit in enumerate(shown):
            x, cw = 50, 1820
            covered = bool(hit.get("covered"))
            card(p, x, y, cw, rowh, fill=C_PANEL2 if covered else C_PANEL)
            col = C_DIM2 if covered else sev_color(hit["sev"])
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(col))
            p.drawRoundedRect(QRect(x, y, 5, rowh), 3, 3)

            draw_text(p, x + 22, y + 10, hit["title"], 22,
                      C_DIM if covered else C_TEXT, True, wbox=620)
            pill(p, x + 660, y + 12, hit["sev"], 15,
                 fill=QColor(28, 34, 44), fg=col, border=col, h=28)
            draw_text(p, x + 760, y + 14, "×%d" % hit["n"], 18, C_DIM, wbox=70)

            if covered:
                cts = int(hit.get("cover_ts") or 0)
                mark = "扫描前 · 已被 %s 的 0 坏块校验覆盖" % time.strftime("%H:%M", time.localtime(cts)) if cts \
                       else "扫描前 · 已被 0 坏块校验覆盖"
                draw_text(p, x + cw - 720, y + 14, mark, 15, C_DIM2, align="right", wbox=700)
            else:
                lt = int(hit.get("last_ts") or 0)
                if lt:
                    draw_text(p, x + cw - 420, y + 14, "最近 %s" % time.strftime("%m-%d %H:%M", time.localtime(lt)),
                              15, C_DIM2, align="right", wbox=400)

            p.setFont(font(17))
            p.setPen(QPen(C_DIM if covered else C_TEXT))
            p.drawText(QRect(x + 22, y + 40, cw - 44, 22), Qt.AlignLeft | Qt.AlignVCenter,
                       elide(p, hit["human"] or "", cw - 44))

            p.setFont(font(15))
            p.setPen(QPen(C_DIM2 if covered else C_BLUE))
            adv = hit["advice"] or ""
            p.drawText(QRect(x + 22, y + 63, cw - 44, 20), Qt.AlignLeft | Qt.AlignVCenter,
                       elide(p, ("建议：%s" % adv) if adv else "", cw - 44))

            y += rowh + 6

        if len(hits) > len(shown):
            draw_text(p, 50, 940, "另有 %d 类命中未显示，命令行 renhua --top 可看全部"
                      % (len(hits) - len(shown)), 17, C_DIM2, align="right", wbox=W - 100)

        ver = snap.get("verified") or {}
        if ver:
            cts = cover_ts_of(ver)
            names = "、".join(sorted(ver))
            tail = "（%s 完成，0 坏块）" % time.strftime("%m-%d %H:%M", time.localtime(cts)) if cts else ""
            draw_text(p, 50, 962, "已全盘校验通过%s：%s　—— 灰条 = 该条最后出现于校验之前，已被覆盖"
                      % (tail, names), 17, C_GREEN)


# =====================  控制通道（给飞牛 Web 面板用） =====================
CTRL_PORT = int(os.environ.get("RENHUA_CTRL_PORT", "8771"))
PAGE_NOW = {"n": 4}
PAGE_NAMES = ["F1 帮助", "F2 坏块扫描", "F3 端口识别", "F4 健康体检", "F5 报错翻译", "F6 测速"]


def ctrl_status():
    snap = SNAP.get()
    names = [c["name"] for c in snap.get("cands", [])]
    smart = snap.get("smart") or {}
    disks = []
    for d in snap.get("disks", []):
        s = smart.get(d["name"]) or {}
        disks.append({
            "name": d["name"], "model": d.get("model", ""), "sn": d.get("sn", ""),
            "size_gb": round(d.get("size_gb") or 0, 1), "ata": d.get("ata"),
            "spd": fmt_spd(d.get("spd")), "state": d.get("state", ""),
            "health": s.get("_health", ""), "temp": s.get("温度"),
            "realloc": s.get("重映射扇区"), "pend": s.get("待映射扇区"),
            "unc": s.get("离线不可纠正"), "crc": s.get("接口 CRC"),
            "hours": s.get("通电小时"),
            "verified": d["name"] in (snap.get("verified") or {}),
            "can_scan": d["name"] in names,
        })
    try:
        host = open("/proc/sys/kernel/hostname").read().strip()
    except OSError:
        host = ""
    return {
        "host": host,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "page": PAGE_NOW["n"], "pages": PAGE_NAMES,
        "disks": disks,
        "links": snap.get("links") or {},
        "scan": snap.get("scan") or {},
        "scan_advice": snap.get("scan_advice") or "",
        "speed": snap.get("speed") or {},
        "hits": snap.get("hits") or [],
        "hits_time": snap.get("hits_time") or 0,
        "verified": snap.get("verified") or {},
        "msg": snap.get("msg") or "",
        "stages": [s[1] for s in STAGES],
    }


def ctrl_handle(obj):
    cmd = str(obj.get("cmd") or "")
    if cmd in ("status", "ping"):
        return {"ok": True, "data": ctrl_status()}
    if cmd == "page":
        try:
            n = int(obj.get("page", 0))
        except (TypeError, ValueError):
            return {"ok": False, "detail": "page 参数不对"}
        n = max(0, min(len(PAGE_NAMES) - 1, n))
        KEYQ.put(("page", n))
        return {"ok": True, "detail": "已切到 %s" % PAGE_NAMES[n]}
    if cmd == "scan_start":
        ok, detail = start_scan(str(obj.get("dev") or ""))
        return {"ok": ok, "detail": detail}
    if cmd == "scan_stop":
        ok, detail = stop_scan()
        return {"ok": ok, "detail": detail}
    if cmd == "speed_start":
        ok, detail = start_speed(str(obj.get("dev") or ""))
        return {"ok": ok, "detail": detail}
    if cmd == "speed_stop":
        ok, detail = stop_speed()
        return {"ok": ok, "detail": detail}
    if cmd == "msg":
        SNAP.set(msg=str(obj.get("text") or ""), msg_time=time.time())
        return {"ok": True, "detail": "已设置"}
    return {"ok": False, "detail": "未知命令：%s" % cmd}


class CtrlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            raw = self.rfile.readline(65536)
            obj = json.loads(raw.decode("utf-8")) if raw.strip() else {"cmd": "status"}
            res = ctrl_handle(obj)
        except Exception as ex:
            res = {"ok": False, "detail": "%s: %s" % (type(ex).__name__, ex)}
        try:
            self.wfile.write((json.dumps(res, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass


class CtrlServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def start_ctrl_server():
    try:
        srv = CtrlServer(("127.0.0.1", CTRL_PORT), CtrlHandler)
    except OSError as ex:
        print("ctrl server 起不来：%s" % ex, file=sys.stderr)
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("ctrl server: 127.0.0.1:%d" % CTRL_PORT, file=sys.stderr)
    return srv


def main():
    # 单实例：两个控制台同时写 /dev/fb0 会让画面在两份内容间来回跳（看起来像闪）
    import fcntl
    lock_path = "/var/run/renhua-console.lock"
    try:
        _lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError) as ex:
        print("another console already owns the screen, exit (%s)" % type(ex).__name__,
              file=sys.stderr)
        sys.exit(0)

    page = None
    for i, a in enumerate(sys.argv):
        if a == "--page" and i + 1 < len(sys.argv):
            try:
                page = max(0, min(5, int(sys.argv[i + 1]) - 1))
            except ValueError:
                pass
        elif a.startswith("--page="):
            try:
                page = max(0, min(5, int(a.split("=", 1)[1]) - 1))
            except ValueError:
                pass

    # 没给 --page 就按 /etc/renhua/renhua.conf 的 page=（和 renhua-screen-mode 保持同一口径）
    if page is None:
        page = 4
        try:
            for line in open("/etc/renhua/renhua.conf"):
                line = line.strip()
                if line.startswith("page="):
                    page = max(0, min(5, int(line.split("=", 1)[1].strip()) - 1))
        except Exception:
            pass

    app = QApplication(sys.argv)
    global FBW, FBH
    FBW, FBH = detect_fb_size()
    print("fb: %dx%d" % (FBW, FBH), file=sys.stderr)
    # 包内字体直接注册，避免系统没有中文字体时显示方块
    ttc = os.path.join(_BUNDLE_FONT_DIR, "wqy-microhei.ttc")
    if os.path.isfile(ttc):
        try:
            fid = QFontDatabase.addApplicationFont(ttc)
            fam = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
            if fam:
                globals()["FONT_FAMILY"] = str(fam[0])
                print("bundle font: %s" % FONT_FAMILY, file=sys.stderr)
        except Exception as ex:
            print("bundle font failed: %s" % type(ex).__name__, file=sys.stderr)
    # 确认字体可用
    fams = QFontDatabase().families()
    if FONT_FAMILY not in [str(f) for f in fams]:
        for cand in ("WenQuanYi Micro Hei", "WenQuanYi Zen Hei"):
            if cand in [str(f) for f in fams]:
                globals()["FONT_FAMILY"] = cand
                break
    print("fonts: %s" % FONT_FAMILY, file=sys.stderr)

    threading.Thread(target=collector_loop, daemon=True).start()
    threading.Thread(target=keyboard_loop, daemon=True).start()
    PAGE_NOW["n"] = page
    start_ctrl_server()

    w = Console(page)
    w.showFullScreen()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

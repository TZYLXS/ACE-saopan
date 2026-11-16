# ACE扫盘工具

飞牛 fnOS 存储故障处置插件：把 `dmesg` / `journal` 里那些看不懂的存储报错翻成「原文 + 人话 + 建议 + 严重度」，同时提供一块**不用桌面、直接写 framebuffer 的物理屏控制台**和一个浏览器面板。

- 开发者 / 发布者：**TZYLXS**
- 包名：`renhua`，显示名「存储体检」，版本 1.1.0，架构 `x86_64`
- GitHub 仓库名：`ACE-saopan`（GitHub 只接受 ASCII 仓库名，「ACE扫盘工具」是它的中文名）

## 它干什么

| 入口 | 说明 |
|---|---|
| `renhua` 命令 | 30 条规则扫描 `dmesg` / `journal`，每条给出原文、人话、处置建议、严重度；`renhua --top` 看命中排行，`renhua --rules` 看规则 |
| 物理屏控制台 | PyQt5 + linuxfb 全自绘，直接画 `/dev/fb0`（无桌面、无 X），evdev 独占键盘切页 |
| Web 面板 | `http://<NAS>:8770`，与物理屏同一个控制台的数据源，可切页、起停扫描与测速 |

物理屏页面：

| 键 | 页面 | 内容 |
|---|---|---|
| F1 | 帮助 | 快捷键、命令速查、规则概览 |
| F2 | 坏块扫描 | 选盘 → `badblocks` 只读全盘扫描，进度条 + 实时速率 + ATA 端口灯条；扫完 0 坏块自动落 `verified.json` 消警 |
| F3 | 端口识别 | ATA1–6 每个口的盘：型号、序列号、容量、链路速率、温度、SMART、CRC |
| F4 | 健康体检 | 每块盘的温度 / 通电时间 / 待映射 / 不可纠正 / CRC，已校验的盘标绿 |
| F5 | 报错翻译 | 近 24 小时命中项，行式列表（标题 + 严重度 + 次数 + 人话 + 建议）；扫描前就存在的标灰并注明「已被 HH:MM 的 0 坏块校验覆盖」，扫描后新增的照常红色 |
| F6 | 测速 | 顺序读/写 1GB、小文件 32MB、零碎 32MB、RND4K Q1T1，八阶段依次跑 |

## 安装

在飞牛 fnOS 的「应用中心 → 手动安装」里选择构建出的 `扫盘.fpk`。安装脚本会自动检查并按需补装依赖：

`python3`、`python3-pyqt5`、`fonts-wqy-microhei`、`smartmontools`、`e2fsprogs`

安装后面板在 `8770`，控制台直接接管 `/dev/fb0`；两者由 `cmd/main` 统一起停（同一份 PID 文件 + flock 单实例，杜绝双进程同写 framebuffer 造成的画面闪烁）。

## 自己构建

只需要 python3（不依赖 `fnpack`），打包就是 tarfile + gzip：

```bash
# 1. 内置字体（可选；不内置则运行时回退到系统字体）
cp /usr/share/fonts/truetype/wqy/wqy-microhei.ttc assets/

# 2. 生成图标（Windows PowerShell；也可用任何工具做 64/256 两档 PNG，放到 src/ui/images/）
#    icon_64.png / icon_256.png

# 3. 打包 + 校验
python3 build-fpk.py      # 产出 dist/扫盘.fpk
python3 verify-fpk.py     # 打印外层清单、manifest 字段与 checksum 核对
```

`manifest` 里的 `checksum` 由 `build-fpk.py` 写入，等于 `app.tgz` 的 md5；`verify-fpk.py` 会实测比对。

## 目录

```
build-fpk.py            打包脚本（相对路径，clone 即可用）
verify-fpk.py           包结构与 checksum 校验
make-icons.ps1          生成 ui/images 的 64/256 图标
src/
  renhua-console.py     物理屏控制台（PyQt5 linuxfb，F1–F6 全自绘）
  renhua-web.py         8770 面板（标准库 ThreadingHTTPServer，代理 127.0.0.1:8771 控制通道）
  renhua                CLI 报错翻译（规则表 + 严重度 + 建议）
  web/                  面板前端（index.html / app.js / styles.css）
  ui/config             桌面入口（iframe 应用，icon 模板 images/icon_{0}.png）
pkg/
  manifest              fnOS 应用清单（maintainer/distributor = TZYLXS）
  cmd/                  安装/启动/停止/卸载生命周期脚本
  wizard/install        安装向导提示（含风险确认勾选项）
assets/                 内置字体与授权说明（字体本体不入库）
```

## 运行环境

- 目标系统：飞牛 fnOS / Debian 12，x86_64，内核自带 framebuffer（`/dev/fb0` 可写）
- 物理屏：`QT_QPA_PLATFORM=linuxfb:fb=/dev/fb0`，分辨率自适应（读 `/sys/class/graphics/fb*/virtual_size`，逻辑坐标固定 1920×1080 后整帧缩放一次贴屏）
- 键盘：`/dev/input/event0` 等设备独占（`EVIOCGRAB`），无键盘时自动跳过
- Web 面板：标准库实现，无第三方依赖（无需 pip）
- 控制通道：`127.0.0.1:8771`，仅本机；面板通过它与控制台交换页面/扫描/测速指令

## 字体

内置字体为**文泉驿微米黑**（`wqy-microhei.ttc`，Apache License 2.0，见 `assets/FONT-NOTICE.txt`），为的是在没有中文字体的 fnOS 上也能正常显示。仓库不包含字体二进制，构建时从系统 `fonts-wqy-microhei` 拷贝即可（见上）。

## 许可

代码部分可自由使用与修改；`assets/FONT-NOTICE.txt` 涉及的字体遵循 Apache License 2.0。

# ⚠️ 本文件由项目脚本自动脱敏生成：本机路径 → <PLUGIN_DIR>/<PROJECT_DIR>，
#    真实 QQ 号 → 10000001/10000002，人设名 → bot/user。
#    逻辑与原版逐字节相同，仅替换了标识。
#!/usr/bin/env pythonw.exe
# -*- coding: utf-8 -*-
"""
dsh-onebot-qq 手工改动恢复脚本

背景：pnpm add/install 会重装 node_modules 里的包，把手工改动清掉。
本脚本把 ~/.dsh/plugins/dsh-onebot-qq-custom 里的定制文件复制回去。

用法：
  - 手动：pythonw restore-onebot.pyw
  - 装完插件后：跑一次
  - 也可加进计划任务（检测到丢失就自动恢复）
"""
import shutil
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(r"<BACKUP_DIR>")
DST = Path(r"<PLUGIN_DIR>")
LOG = Path(r"<PROJECT_DIR>\backup\onebot-restore.log")

# 需要恢复的定制文件（相对路径）
#
# 2026-11-15 升级 0.10.0 后的结构变化：
#   官方把入站防抖搬进了独立的 lib/inbound-queue.js，所以它是**新增的第 6 个
#   定制文件**。少了它，打字感知完全失效（bridge 会调用不存在的 noteTyping）。
#
# 两类文件的区别（重要）：
#   · lib/inbound-queue.js 是**全量重写**（707 行里 ~200 行是我的），
#     直接整文件复制即可。
#   · 其余 5 个 lib/*.js 是**增量补丁**（在官方文件上插入若干块），
#     升级官方版本后必须用 scripts\patch-onebot.py 重新打补丁，
#     **不能**整文件覆盖 —— 否则会把旧官方的代码搬进新版本。
CUSTOM_FILES = [
    "lib/bridge.js",
    "lib/connection.js",
    "lib/index.js",
    "lib/outbound.js",
    "lib/inbound.js",
    "lib/inbound-queue.js",
    "linshuning-declarer.mjs",
    "cordis.patch.yml",
    "presets/lin-shuning/agent.cordis.yml",
    "presets/lin-shuning/persona-live.mjs",
    "presets/lin-shuning/preset.json",
    "presets/lin-shuning/preset.yml",
]

# 全量重写的文件：备份与生产必须**逐字节相同**，直接覆盖最安全。
# 增量补丁的文件：用标记检测，不比对 hash（官方升级会改变无关行）。
WHOLE_FILE = {
    "lib/inbound-queue.js",
    "presets/lin-shuning/persona-live.mjs",
    "linshuning-declarer.mjs",
}

# 校验标记：文件里必须包含这些字符串才算"改动在位"
MARKERS = {
    # 2026-11-15：升级 dsh-onebot-qq 0.10.0 后标记全部重定。
    # 挑的都是「官方版本绝不会有、只有我的补丁才会写进去」的字符串，
    # 这样被 pnpm 覆盖回官方文件时必然命中。
    "lib/bridge.js": "appendRecentChat",
    "lib/connection.js": "onTypingNotice",
    "lib/index.js": "typingAwareDebounce",
    "lib/outbound.js": "recentlySentSimilar",
    "lib/inbound.js": "faceToEmoji",
    # 官方文件的 InboundScheduler 没有 noteTyping —— 这一项是打字感知的命门
    "lib/inbound-queue.js": "noteTyping",
    "cordis.patch.yml": "agentPreset",
    "linshuning-declarer.mjs": "lin-shuning",
    "presets/lin-shuning/agent.cordis.yml": "persona-live.mjs",
    "presets/lin-shuning/persona-live.mjs": "buildMemory",
}


def installed_version() -> str:
    """读安装目录里官方 package.json 的 version。读不到返回空串。"""
    try:
        import json
        data = json.loads((DST / "package.json").read_text(encoding="utf-8"))
        return str(data.get("version", ""))
    except Exception:
        return ""


def version_mismatch() -> tuple[str, str]:
    """返回 (安装版本, 备份记录版本)。两者不同就该停下人工确认。

    为什么要有这道闸（2026-11-15 事故教训）：
      本脚本的职责是「官方包被 pnpm 覆盖后把补丁贴回去」。但如果官方**升级了
      大版本**（例如 0.10.0 → 0.11.0），把 0.10.0 的补丁文件整体覆盖到 0.11.0
      上会留下「新官方 + 旧补丁」的混合安装 —— 这正是 2026-11-13 升级 DSH 时
      把程序装坏的那类事故（半旧半新，直接起不来）。
      所以版本对不上时**只告警不写入**，等人工重新合并补丁。
    """
    cur = installed_version()
    try:
        recorded = (SRC / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        recorded = ""
    return cur, recorded


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def md5(path: Path) -> str:
    import hashlib
    h = hashlib.md5()
    h.update(path.read_bytes())
    return h.hexdigest()


def needs_restore() -> list[str]:
    """返回需要恢复的文件列表（缺失、标记丢失、或全量重写文件内容不一致）"""
    todo = []
    for rel, marker in MARKERS.items():
        target = DST / rel
        src = SRC / rel
        if not target.exists():
            todo.append(rel + " (缺失)")
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
            if marker not in text:
                todo.append(rel + " (改动丢失)")
                continue
        except Exception:
            todo.append(rel + " (读取失败)")
            continue
        # 全量重写的文件：内容必须与备份逐字节相同，否则说明被官方版替换了
        # （这种情况标记可能还在 —— 比如官方后来也加了同名函数 —— 但行为已经变）
        if rel in WHOLE_FILE and src.exists():
            try:
                if md5(target) != md5(src):
                    todo.append(rel + " (内容与备份不一致)")
            except Exception:
                todo.append(rel + " (hash 读取失败)")
    return todo


def main() -> None:
    if not SRC.exists():
        log("跳过：备份目录不存在 %s" % SRC)
        return
    if not DST.exists():
        log("跳过：目标目录不存在 %s" % DST)
        return

    todo = needs_restore()
    if not todo:
        return  # 全部在位，静默退出

    # ⚠️ 版本守门：官方版本与备份记录的版本不一致时，绝不写入。
    # 详见 version_mismatch() 的 docstring（2026-11-13 事故教训）。
    cur, recorded = version_mismatch()
    if recorded and cur and cur != recorded:
        log("⚠ 版本不一致，已中止恢复：安装=%s 备份=%s" % (cur, recorded))
        log("  请先按新版本重新合并补丁，然后更新备份里的 VERSION 文件")
        return

    log("检测到需要恢复: %s" % ", ".join(todo))

    restored = 0
    for rel in CUSTOM_FILES:
        s = SRC / rel
        d = DST / rel
        if not s.exists():
            continue
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            restored += 1
        except PermissionError as e:
            log("  权限不足（文件被占用？）: %s — %s" % (rel, e))
        except Exception as e:
            log("  复制失败: %s — %s" % (rel, e))

    log("恢复完成：%d 个文件" % restored)

    # 校验
    still = needs_restore()
    if still:
        log("⚠ 仍有问题: %s" % ", ".join(still))
    else:
        log("✓ 全部改动已恢复到位")


if __name__ == "__main__":
    main()

# ⚠️ 本文件由项目脚本自动脱敏生成：本机路径 → <PLUGIN_DIR>/<PROJECT_DIR>，
#    真实 QQ 号 → 10000001/10000002，人设名 → bot/user。
#    逻辑与原版逐字节相同，仅替换了标识。
#!/usr/bin/env pythonw.exe
# -*- coding: utf-8 -*-
"""
bot链路监控（无窗口版）
用 pythonw.exe 运行，完全不弹命令行窗口。
由 Windows 计划任务每 5 分钟调用一次。

=== 判据（任一成立即视为异常）===
    1. 3002 未监听
    2. 3002 在听但没有客户端连接
    3. ★ 本次启动后 Login Error 次数 >= FAIL_THRESHOLD（默认 10）

=== 分级处置（2026-11-08 第二次修正）===
    过去的做法是「失败达阈值 → 直接停服，等人扫码」。**这是错的。**

    2026-11-08 的真实过程证明：那 184 次失败是「旧 Worker 卡死」，
    不是「登录态彻底失效」。NapCat 检测到离线后重启 Worker，
    新 Worker **复用了本地登录态直接上线，根本不需要扫码**
    （证据：10:25:43 收到真实消息 `接收 <- 临时消息`）。

    所以正确顺序是「先自愈，后升级」：

        第 1 级  失败达阈值        → systemctl restart napcat（重启 Worker 试试自愈）
        第 2 级  重启后仍失败      → 再重启，最多 MAX_RESTARTS 次
        第 3 级  重启次数用尽      → systemctl stop napcat（止血，等人工扫码）

    「stop」是**最后手段**，不是第一反应。

=== 保护措施 ===
    - GRACE_SEC    服务刚启动的宽限期内不动它（留给启动/扫码）
    - RESTART_COOLDOWN_SEC  两次重启之间至少间隔（防抖，避免疯狂重启）
    - MAX_RESTARTS 每天最多自动重启次数（用尽后转人工）
    - 失败数只统计「本次服务启动后」，不是全天累计
      （否则今天失败过 184 次之后，之后每次启动都会立刻被判异常）

=== 手动操作 ===
    阈值/宽限/次数都在下方常量区。改完直接生效（计划任务每 5 分钟重跑）。
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(r"<PROJECT_DIR>")
STATE = BASE / "backup" / "link-state.json"
LOG = BASE / "backup" / "link-monitor.log"

FAIL_THRESHOLD = 10          # 「本次启动后」Login Error 达到这个数就触发处置
GRACE_SEC = 120              # 服务刚启动的宽限期内不动它（秒）
RESTART_COOLDOWN_SEC = 300   # 两次自动重启最小间隔（秒），防抖
MAX_RESTARTS = 3             # 同一轮故障最多自动重启次数，用尽转人工停服
CREATE_NO_WINDOW = 0x08000000

# ── GitHub 备份同步（2026-11-15 并入本脚本）──────────────────────────
#
# 用户要求：「把那个检测 github 同步的定时任务合并到检查链路的定时任务内」。
#
# 为什么放在这里而不是单独建任务：
#   本脚本已经每 5 分钟跑一次，再加一个 5 分钟级的计划任务纯属浪费。
#   而且「链路检查」和「备份同步」都是运维巡检，属同一类职责。
#
# 为什么不能每 5 分钟都推：
#   那会产生大量空提交和磁盘 churn。所以用两道闸：
#   ① 先跑 `--quick` 问一句「有没有改动」，没改动直接返回（耗时约 1 秒）
#   ② 即便有改动，也要距上次同步 >= SYNC_MIN_INTERVAL_SEC 才真的推
#
# 为什么不用计划任务里配 `--public` 之类：
#   两个仓库都要同步，直接跑全量。
SYNC_MIN_INTERVAL_SEC = 6 * 3600   # 两次真实同步之间至少隔 6 小时
SYNC_SCRIPT = Path(r"<HOME>\Desktop\dsh\自主智能体\_github\sync-to-github.py")
SYNC_STATE_KEY = "githubSyncAt"    # 存在 link-state.json 里，跨轮次累计


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def toast(title: str, body: str) -> bool:
    """Windows 10/11 原生通知（通过 PowerShell 调用，但用 -WindowStyle Hidden）"""
    try:
        # 单引号转义，避免标题/正文里的引号破坏 PowerShell 字符串
        t = str(title).replace("'", "''")
        b = str(body).replace("'", "''")
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
            "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$x=$t.GetElementsByTagName('text');"
            f"$x.Item(0).AppendChild($t.CreateTextNode('{t}'))|Out-Null;"
            f"$x.Item(1).AppendChild($t.CreateTextNode('{b}'))|Out-Null;"
            "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('bot').Show($n)"
        )
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, timeout=20,
            creationflags=CREATE_NO_WINDOW,
        )
        return True
    except Exception as e:
        log(f"toast failed: {e}")
        return False


def wsl(cmd: str, timeout: int = 30) -> str:
    """跑一条 WSL 命令，返回 stdout（失败返回空串）"""
    try:
        r = subprocess.run(
            ["wsl", "-d", "Ubuntu", "--", "bash", "-c", cmd],
            capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8", errors="replace",
        )
        return r.stdout or ""
    except Exception as e:
        log(f"wsl cmd failed: {e}")
        return ""


def check_status() -> dict:
    """返回 {listening, conns, svc, failures, uptime_sec}

    failures 用「**本次服务启动之后**的 Login Error 次数」，不是全天累计。

    为什么（2026-11-08 两次踩坑）：
        坑1：用全天累计 → 今天失败过 184 次后，每次启动服务都被立刻判异常，
             用户永远扫不了码。
        坑2：改用 awk 比较时间戳 → **mktime() 对 "Sun 2026-11-08 10:32:05 CST"
             返回 -1**（不支持这种格式），于是「所有行都 >= -1」，统计出 725 次
             的假数字。用 30 秒才发现。

    最终做法：**按行号切分，完全不解析时间**。
        找最后一次「OneBot11 适配器初始化完成」的行号（每次服务启动必打这一行），
        只统计该行之后的 Login Error。简单、无格式依赖、不会算错。
    """
    out = wsl(
        # 1) 3002 监听数
        "ss -tln 2>/dev/null | grep -c ':3002' ; echo '---' ; "
        # 2) 3002 已建立连接数
        "ss -tn 2>/dev/null | grep -c 3002 ; echo '---' ; "
        # 3) 服务状态
        "systemctl is-active napcat 2>/dev/null ; echo '---' ; "
        # 4) 服务本次已运行秒数（-1 = 没在跑）
        "systemctl show napcat -p ActiveEnterTimestamp --value 2>/dev/null | "
        "awk '{ if ($0==\"\") { print -1 } else { "
        "cmd=\"date -d \\\"\" $0 \"\\\" +%s\"; cmd | getline t; close(cmd); "
        "if (t>0) print systime()-t; else print -1 } }' ; echo '---' ; "
        # 5) ★ 按行号切分：只数「最后一次服务启动」之后的 Login Error
        "L=/home/hatsuyuki/napcat-work/service.log; "
        "if [ ! -f \"$L\" ]; then echo 0; else "
        "LN=$(grep -an 'OneBot11 适配器初始化完成' \"$L\" 2>/dev/null | tail -1 | cut -d: -f1); "
        "if [ -z \"$LN\" ]; then "
        # 找不到启动标记（比如从未成功启动）→ 退化为全天计数
        "grep -a \"$(date +%m-%d)\" \"$L\" 2>/dev/null | grep -ac 'Login Error'; "
        "else tail -n +$LN \"$L\" 2>/dev/null | grep -ac 'Login Error'; fi; fi"
    )
    parts = [p.strip() for p in out.split("---")]

    def to_int(s: str) -> int:
        s = (s or "").strip()
        try:
            return int(s)
        except ValueError:
            return 0

    return {
        "listening": to_int(parts[0]) if len(parts) > 0 else 0,
        "conns": to_int(parts[1]) if len(parts) > 1 else 0,
        "svc": (parts[2].strip() if len(parts) > 2 else "unknown") or "unknown",
        "uptime_sec": to_int(parts[3]) if len(parts) > 3 else -1,
        "failures": to_int(parts[4]) if len(parts) > 4 else 0,
    }


def stop_napcat() -> bool:
    """最后手段：停服（不重启）。返回是否确认已停。"""
    wsl("sudo systemctl stop napcat", timeout=60)
    st = check_status()
    ok = st["svc"] not in ("active", "activating", "reloading")
    log(f"止血：stop napcat → svc={st['svc']} 确认={ok}")
    return ok


def restart_napcat() -> bool:
    """第 1/2 级手段：重启 Worker，尝试复用本地登录态自愈。

    2026-11-08 实证：重启后新 Worker 会绕过卡死的旧 Worker，
    直接复用本地登录态上线，**不需要扫码**。
    返回是否确认已重新 active。
    """
    wsl("sudo systemctl restart napcat", timeout=90)
    st = check_status()
    ok = st["svc"] in ("active", "activating")
    log(f"重启自愈：restart napcat → svc={st['svc']} 确认={ok}")
    return ok


def recover(prev: dict, st: dict, failures: int) -> dict:
    """分级处置：先重启自愈，重启用尽才停服。

    返回 {action, restarts, last_restart_at, note}
      action: 'restart' | 'stop' | 'none'
    """
    now_ts = datetime.now().timestamp()
    restarts = int((prev or {}).get("restarts") or 0)
    last_at = float((prev or {}).get("lastRestartAt") or 0)

    # 冷却期内不重复动作（防抖）
    if last_at and (now_ts - last_at) < RESTART_COOLDOWN_SEC:
        return {"action": "none", "restarts": restarts,
                "last_restart_at": last_at,
                "note": f"冷却中（距上次动作 {int(now_ts-last_at)}s < {RESTART_COOLDOWN_SEC}s）"}

    if restarts < MAX_RESTARTS:
        ok = restart_napcat()
        return {"action": "restart", "restarts": restarts + 1,
                "last_restart_at": now_ts,
                "note": f"第 {restarts+1}/{MAX_RESTARTS} 次重启自愈" + ("" if ok else "（未确认 active）")}

    # 次数用尽 → 停服，等人工
    ok = stop_napcat()
    return {"action": "stop", "restarts": restarts,
            "last_restart_at": now_ts,
            "note": "重启次数已用尽，停服等待人工扫码" + ("" if ok else "（停服未确认）")}


def _safe_float(v, default=0.0) -> float:
    """把任意值安全转成 float。

    为什么需要：link-state.json 是磁盘文件，可能被外部改坏、被旧版本写成字符串、
    或断电时写了一半。`float("not-a-number")` 会抛 ValueError，
    而这里的调用方是每 5 分钟跑一次的监控脚本 —— 一个坏字段不能让整个监控挂掉。
    """
    try:
        f = float(v)
        # NaN / inf 也当作无效（JSON 里不该出现，但防御一下）
        if f != f or f in (float("inf"), float("-inf")):
            return default
        return f
    except (TypeError, ValueError):
        return default


def sync_github(prev: dict) -> dict:
    """按需把项目改动同步到两个 GitHub 仓库。返回更新后的同步状态。

    绝不抛异常：备份失败不能影响链路监控本身。
    三道闸：
      ① 脚本不存在就跳过
      ② `--quick` 说没改动就跳过（耗时约 1 秒）
      ③ 距上次同步不足 SYNC_MIN_INTERVAL_SEC 就跳过
    """
    import time

    result = {
        SYNC_STATE_KEY: _safe_float((prev or {}).get(SYNC_STATE_KEY)),
        "githubSyncResult": str((prev or {}).get("githubSyncResult") or ""),
    }

    if not SYNC_SCRIPT.exists():
        return result

    last = result[SYNC_STATE_KEY]
    elapsed = time.time() - last
    if last and elapsed < SYNC_MIN_INTERVAL_SEC:
        # 还没到间隔，连 --quick 都不用跑
        return result

    try:
        # ── 闸②：先问一句有没有改动（退出码 10 = 有改动）──
        quick = subprocess.run(
            [sys.executable, str(SYNC_SCRIPT), "--quick"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=CREATE_NO_WINDOW,
        )
        if quick.returncode == 0:
            return result                     # 无改动，静默跳过
        if quick.returncode != 10:
            log("GitHub 同步预检异常：退出码 %d %s"
                % (quick.returncode, (quick.stdout or "").strip()[:120]))
            return result

        # ── 真的同步 ──
        log("检测到项目改动，开始同步 GitHub：" + (quick.stdout or "").strip()[:120])
        r = subprocess.run(
            [sys.executable, str(SYNC_SCRIPT)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, creationflags=CREATE_NO_WINDOW,
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()

        if r.returncode == 0 and "脱敏检查未通过" not in out:
            result[SYNC_STATE_KEY] = time.time()
            # 只在真的推了东西时才记「已推送」，避免无改动也写一行
            pushed = "已推送" in out
            result["githubSyncResult"] = "pushed" if pushed else "nochange"
            log("GitHub 同步完成（%s）" % ("已推送" if pushed else "无实质改动"))
        elif r.returncode == 2 or "脱敏检查未通过" in out:
            # 脱敏闸拦截：这是保护机制生效，不是故障，但必须让人知道
            result["githubSyncResult"] = "blocked"
            log("⚠ GitHub 同步被脱敏检查拦下（保护机制生效，需人工修 build-public-repo.py）")
            toast("GitHub 同步被拦下", "脱敏检查发现敏感串，已按设计中止推送")
        else:
            result["githubSyncResult"] = "error"
            log("GitHub 同步失败（退出码 %d）：%s" % (r.returncode, out[-300:]))
    except subprocess.TimeoutExpired:
        result["githubSyncResult"] = "timeout"
        log("GitHub 同步超时（>600s），下轮再试")
    except Exception as e:
        result["githubSyncResult"] = "exc"
        log("GitHub 同步异常：%s" % e)

    return result


def main() -> None:
    st = check_status()
    listening, conns, svc, failures = st["listening"], st["conns"], st["svc"], st["failures"]

    # 综合判定
    if listening >= 1 and conns >= 1 and failures < FAIL_THRESHOLD:
        online, reason = True, ""
    elif failures >= FAIL_THRESHOLD:
        online = False
        # 区分「还在刷」和「已停服、在等扫码」——后者是已知状态，不用反复报
        if svc in ("active", "activating", "reloading"):
            reason = f"登录失败 {failures} 次（阈值 {FAIL_THRESHOLD}），正在刷登录"
        else:
            reason = f"登录失败 {failures} 次，已停服待扫码"
    elif listening >= 1:
        online, reason = False, "3002 监听中但无客户端连接"
    else:
        online, reason = False, f"3002 未监听 (napcat={svc})"

    prev = None
    try:
        if STATE.exists():
            prev = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        pass

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state = {
        "online": online,
        "since": (prev or {}).get("since", now) if not online else now,
        "reason": reason,
        "failures": failures,
        "svc": svc,
        "checkedAt": now,
        # 分级处置的持久化状态（跨轮次累计，用于 MAX_RESTARTS 与冷却）
        "restarts": int((prev or {}).get("restarts") or 0),
        "lastRestartAt": float((prev or {}).get("lastRestartAt") or 0),
        # GitHub 同步状态（必须在这里带上，否则回写时会丢）
        # 用 _safe_float：状态文件可能被写坏，坏值不能拖垮监控
        "githubSyncAt": _safe_float((prev or {}).get("githubSyncAt")),
        "githubSyncResult": str((prev or {}).get("githubSyncResult") or ""),
    }

    if online:
        # 上线了 → 重置重启计数，下一轮故障重新计数
        state["restarts"] = 0
        state["lastRestartAt"] = 0
        if prev and prev.get("online") is False:
            log(f"恢复在线（此前 {prev.get('since','?')} 起离线，原因 {prev.get('reason','?')}）")
            toast("bot已上线", f"链路恢复（{prev.get('since','?')} 起离线）")
        elif prev is None:
            log("首次检查：在线")
    else:
        is_new_incident = (prev is None) or (prev.get("online") is True)
        in_grace = 0 <= st.get("uptime_sec", -1) < GRACE_SEC

        # ★ 分级处置：失败达阈值且服务还活着 → 先重启自愈，重启用尽才停服
        act = {"action": "none", "restarts": state["restarts"],
               "last_restart_at": state["lastRestartAt"], "note": ""}
        if failures >= FAIL_THRESHOLD and svc in ("active", "activating", "reloading"):
            if in_grace:
                act["note"] = (f"失败 {failures} 次达阈值，但服务仅启动 "
                               f"{st.get('uptime_sec')}s（宽限 {GRACE_SEC}s 内），先不动")
                log(act["note"])
            else:
                log(f"⚠ 本次启动后登录失败 {failures} 次 >= {FAIL_THRESHOLD}，进入分级处置")
                act = recover(prev, st, failures)
                state["restarts"] = act["restarts"]
                state["lastRestartAt"] = act["last_restart_at"]
                if act["action"] == "stop":
                    state["svc"] = "inactive"
                reason += " | " + act["note"]
                state["reason"] = reason

        if is_new_incident:
            log(f"掉线: {reason}")
            body = reason
            if act["action"] == "restart":
                body += " | 已自动重启，观察中"
            elif act["action"] == "stop":
                body += " | 已停服，需手机扫码"
            else:
                body += " | 需检查"
            toast("bot掉线了", body)
        else:
            prev_f = int((prev or {}).get("failures") or 0)
            crossed = (failures // FAIL_THRESHOLD) > (prev_f // FAIL_THRESHOLD)
            if act["action"] != "none":
                pass  # restart_napcat / stop_napcat 内部已记录
            elif crossed:
                log(f"仍离线，失败数 {prev_f} → {failures}（跨过新台阶）")
            elif (prev or {}).get("reason") != reason:
                log(f"仍离线: {reason}")

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    # ── GitHub 备份同步（2026-11-15 并入）────────────────────────────
    # 放在状态落盘之后：链路检查先完成并留存，备份同步即便出问题也不影响它。
    # sync_github 内部已 try/except 全覆盖，不会抛出来打断本脚本。
    try:
        sync_state = {
            "githubSyncAt": float(state.get("githubSyncAt") or 0),
            "githubSyncResult": str(state.get("githubSyncResult") or ""),
        }
        new_sync = sync_github(sync_state)
        # 只有确实变化了才回写（避免每轮都重写状态文件）
        if (new_sync.get("githubSyncAt") != sync_state["githubSyncAt"]
                or new_sync.get("githubSyncResult") != sync_state["githubSyncResult"]):
            state.update(new_sync)
            STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        # 兜底：绝不让备份同步的意外破坏链路监控
        log("GitHub 同步外层异常（已忽略）：%s" % e)


if __name__ == "__main__":
    main()

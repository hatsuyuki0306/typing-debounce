# ⚠️ 本文件由项目脚本自动脱敏生成：本机路径 → <PLUGIN_DIR>/<PROJECT_DIR>，
#    真实 QQ 号 → 10000001/10000002，人设名 → bot/user。
#    逻辑与原版逐字节相同，仅替换了标识。
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本项目定制：把全部改动补丁进官方 dsh-onebot-qq 0.10.0。

设计原则：
  1. **幂等**——重复跑不会重复插入（每个补丁点先查 mark 是否已存在）。
  2. **可验证**——每个补丁点插入后立刻回读校验，缺一处就报错退出。
  3. **锚点唯一**——只认官方 0.10.0 的确切文本；找不到锚点就报错，
     绝不"猜着改"，避免官方升级后静默改错地方。

用法：
    python patch-onebot.py            # 应用全部补丁
    python patch-onebot.py --check    # 只检查，不写文件

背景：2026-11-15 升级 0.10.0 时发现 LinShuning-OnebotRestore 定时任务会
把 lib/ 覆盖回 0.9.7，所以改成「官方文件 + 本脚本」的组合，恢复任务以后
必须调用本脚本而不是复制旧文件。
"""
import io
import os
import sys

PLUGIN = os.environ.get(
    "ONEBOT_DIR",
    r"<PLUGIN_DIR>",
)
LIB = os.path.join(PLUGIN, "lib")

CHECK_ONLY = "--check" in sys.argv

errors = []
applied = []


def read(rel):
    path = os.path.join(LIB, rel)
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def write(rel, text):
    path = os.path.join(LIB, rel)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def patch(rel, name, anchor, replacement, mark):
    """在 rel 文件里把 anchor 替换成 replacement。mark 用于幂等检测。

    - 若 mark 已存在 → 视为已打过，跳过
    - 若 anchor 找不到 → 记错误（官方文本变了，人工确认）
    - 否则替换一次（必须唯一）
    """
    text = read(rel)
    if mark in text:
        applied.append("%s :: %s（已存在，跳过）" % (rel, name))
        return
    count = text.count(anchor)
    if count == 0:
        errors.append("%s :: %s —— 锚点找不到，官方文本可能已变化" % (rel, name))
        return
    if count > 1:
        errors.append("%s :: %s —— 锚点出现 %d 次，不唯一" % (rel, name, count))
        return
    if CHECK_ONLY:
        applied.append("%s :: %s（待应用）" % (rel, name))
        return
    write(rel, text.replace(anchor, replacement, 1))
    # 写后回读校验
    if mark not in read(rel):
        errors.append("%s :: %s —— 写入后校验失败" % (rel, name))
        return
    applied.append("%s :: %s（已应用）" % (rel, name))


# ══════════════════════════════════════════════════════════════════════
# 1. bridge.js —— 调度器接线打字感知参数
# ══════════════════════════════════════════════════════════════════════
patch(
    "bridge.js",
    "调度器注入打字感知参数",
    """        this.inboundQueue = deps.config.inboxQueueEnabled === true
            ? new InboundScheduler({
                log: (level, message) => deps.log(level, message),
                deliver: event => this.handleInboundOnce(event, false),
                injectDeliver: event => runInjectRound({ traceId: newTraceId(), intercepted: 0 }, () => this.handleInboundOnce(event, true)),
                ...(deps.config.inboxQueueDelayMs !== undefined ? { mergeWindowMs: deps.config.inboxQueueDelayMs } : {}),
            })
            : undefined;""",
    """        // 本项目定制（2026-11-15 合并进官方架构）：调度器同时承接打字感知防抖。
        // 三道参数在这里注入：quietMs（安静窗口，默认 6500 > NapCat 的 5 秒推送间隔）、
        // typingMaxMs（绝对上限）、typingAware（总开关）。
        this.inboundQueue = deps.config.inboxQueueEnabled === true
            ? new InboundScheduler({
                log: (level, message) => deps.log(level, message),
                deliver: event => this.handleInboundOnce(event, false),
                injectDeliver: event => runInjectRound({ traceId: newTraceId(), intercepted: 0 }, () => this.handleInboundOnce(event, true)),
                ...(deps.config.inboxQueueDelayMs !== undefined ? { mergeWindowMs: deps.config.inboxQueueDelayMs } : {}),
                typingAware: deps.config.typingAwareDebounce !== false,
                ...(typeof deps.config.inboundQuietMs === 'number' && deps.config.inboundQuietMs >= 0
                    ? { quietMs: deps.config.inboundQuietMs }
                    : {}),
                ...(typeof deps.config.inboundTypingMaxMs === 'number' && deps.config.inboundTypingMaxMs > 0
                    ? { typingMaxMs: deps.config.inboundTypingMaxMs }
                    : {}),
            })
            : undefined;""",
    "调度器同时承接打字感知防抖",
)

# ══════════════════════════════════════════════════════════════════════
# 2. bridge.js —— onTypingNotice 入口
# ══════════════════════════════════════════════════════════════════════
patch(
    "bridge.js",
    "onTypingNotice 入口",
    """    async handleInboundOnce(event, injected) {""",
    """    /**
     * 本项目定制（2026-11-14，2026-11-15 合并进官方调度器）：
     * 「对方正在输入」notice 的唯一入口。
     *
     * 只认 private 的 input_status。event_type 语义见 NapCat 源码：
     *   onInputStatusPush → new gAe(core, uin, n.eventType, n.statusText)
     *   statusText 为「对方正在输入...」= 正在打字；空串 = 停止打字。
     * 这里**同时**用 statusText 和 eventType 判断，任一缺失都能工作。
     *
     * 注意：本方法只往调度器写「打字状态」，**绝不直接派发**。
     * 派发时机完全由 InboundScheduler.nextWait() 决定 —— 那里有完整的
     * 两条实测时序陷阱说明（NapCat 5 秒推送 / 停止信号先于消息）。
     */
    onTypingNotice(event) {
        try {
            if (this.deps.config.typingAwareDebounce === false)
                return;                          // 配置关闭 → 完全退回旧行为
            if (event === undefined || event === null)
                return;
            if (event.post_type !== 'notice')
                return;
            if (event.sub_type !== 'input_status')
                return;                          // 其它 notice 一律忽略
            const userId = event.user_id === undefined || event.user_id === null
                ? ''
                : String(event.user_id);
            if (userId === '')
                return;
            // 打字感知只服务私聊：群聊里「谁在打字」无法可靠归属。
            const chatId = buildChatId('private', userId);
            const text = typeof event.status_text === 'string' ? event.status_text : '';
            const isTyping = text.trim() !== '' || event.event_type === 1;
            if (this.inboundQueue === undefined) {
                // 调度器未构造（inboxQueueEnabled 关闭）→ 打字感知无处落地。
                this.deps.log('debug', '收到打字信号但入站调度器未启用，已忽略');
                return;
            }
            this.inboundQueue.noteTyping(chatId, isTyping);
        }
        catch (error) {
            // 打字状态是纯增强：失败只记一行，绝不影响消息主链路
            this.deps.log('debug', 'onTypingNotice 异常: ' + describeError(error));
        }
    }
    async handleInboundOnce(event, injected) {""",
    "onTypingNotice(event) {",
)

# ══════════════════════════════════════════════════════════════════════
# 3. bridge.js —— appendRecentChat + buildMemoryPrefix
# ══════════════════════════════════════════════════════════════════════
patch(
    "bridge.js",
    "appendRecentChat + buildMemoryPrefix",
    """    /** Per-chat outbound-mode override (/mode), falling back to the global config. */
    effectiveInterim(chatId) {""",
    """    /** 2026-11-10 改造：记忆已移入系统提示词（persona-live），这里不再拼接。
     *  旧实现每轮把 mood/episodes/profile/diary 塞进用户消息，和 preset 里的
     *  记忆块**双向重复计费**。现在这里只做一件事：把本轮原话归档到
     *  memory/recent-chat.md，由 persona-live 读进系统提示词。
     *  失败绝不阻断消息：任何异常都静默降级。 */
    buildMemoryPrefix() {
        return '';
    }
    /** 本项目定制：把一条消息原话追加到 workspace/memory/recent-chat.md（滚动归档）。
     *  她的长期记忆里「最近说过的话」这一块就是从这文件读的。
     *  @param speaker 'user' | 'bot'
     *  @param text 原话（已 trim，空串直接忽略）
     */
    appendRecentChat(speaker, text) {
        try {
            const body = String(text ?? '').trim();
            if (body === '')
                return;
            const ws = this.deps.config.workspacePath;
            if (ws === undefined || ws === '')
                return;
            const dir = join(ws, 'memory');
            const file = join(dir, 'recent-chat.md');
            const now = new Date();
            const pad = n => String(n).padStart(2, '0');
            const stamp = pad(now.getMonth() + 1) + '-' + pad(now.getDate())
                + ' ' + pad(now.getHours()) + ':' + pad(now.getMinutes());
            // 单条超过 2000 字多半是粘贴的长文，截断避免把文件撑爆
            const line = '- ' + stamp + ' ' + speaker + '：'
                + (body.length > 2000 ? body.slice(0, 2000) + '…' : body);
            if (!existsSync(dir))
                mkdirSync(dir, { recursive: true });
            let head = '';
            if (!existsSync(file))
                head = '# 最近的原话（滚动归档，由插件自动追加）\\n\\n';
            appendFileSync(file, head + line + '\\n', 'utf8');
        }
        catch (error) {
            this.deps.log('debug', 'recent-chat 归档失败（不影响回复）: ' + describeError(error));
        }
    }
    /** Per-chat outbound-mode override (/mode), falling back to the global config. */
    effectiveInterim(chatId) {""",
    "appendRecentChat(speaker, text) {",
)

# ══════════════════════════════════════════════════════════════════════
# 4. bridge.js —— 强制关闭 interim
# ══════════════════════════════════════════════════════════════════════
patch(
    "bridge.js",
    "强制关闭 interim",
    """    effectiveInterim(chatId) {
        return this.registry.getSettings(chatId).interimOverride ?? this.deps.config.interimMessages;
    }""",
    """    effectiveInterim(chatId) {
        // 本项目定制：**强制关闭 interim（中间态流式）**。
        // 原机制会在生成过程中先发若干条 interim，回合结束时再 delete_msg 撤回、
        // 然后把它们渲染成一张带「1. 2. 3.」序号的汇总卡片——在 QQ 里表现为
        // 「发完消息又撤回重发」+「消息前面多个数字」，像出故障。
        // 她的场景不需要中间态：直接等最终回复、按字数延迟分条发就好。
        return false;
    }""",
    "强制关闭 interim（中间态流式）",
)

# ══════════════════════════════════════════════════════════════════════
# 5. bridge.js —— 用户原话归档
# ══════════════════════════════════════════════════════════════════════
patch(
    "bridge.js",
    "归档用户原话",
    """        this.deps.log('info', 'followup from ' + chatId + ': ' + final.slice(0, 120));""",
    """        this.deps.log('info', 'followup from ' + chatId + ': ' + final.slice(0, 120));
        // 本项目定制（2026-11-10）：把**用户原话**（不是拼了记忆前缀的那份）
        // 归档到 recent-chat.md，她的「最近说过的话」就从这读。
        this.appendRecentChat('user', text);""",
    "归档到 recent-chat.md，她的「最近说过的话」就从这读。",
)

# ══════════════════════════════════════════════════════════════════════
# 6. bridge.js —— 她的话归档
# ══════════════════════════════════════════════════════════════════════
patch(
    "bridge.js",
    "归档她的话",
    """            if (this.deferred.onAssistantMessage(chatId, session.id, event.data.message))
                return;
            this.interim.onAssistantMessage(chatId, chat, event.data.message);""",
    """            if (this.deferred.onAssistantMessage(chatId, session.id, event.data.message))
                return;
            // 本项目定制（2026-11-10）：把**她这一轮说的话**也归档到
            // recent-chat.md。只取 text block 拼起来；工具调用、思考块都不记。
            // 放在 deferred 判断之后 —— 被静音的超时回合不算「她说过的话」。
            try {
                const said = event.data.message.content
                    .filter(block => block.type === 'text' && typeof block.text === 'string')
                    .map(block => block.text)
                    .join('\\n');
                if (said.trim() !== '')
                    this.appendRecentChat('bot', said);
            }
            catch {
                // 归档失败不影响回复
            }
            this.interim.onAssistantMessage(chatId, chat, event.data.message);""",
    "把**她这一轮说的话**也归档到",
)

# ══════════════════════════════════════════════════════════════════════
# 7. bridge.js —— fs 导入
# ══════════════════════════════════════════════════════════════════════
patch(
    "bridge.js",
    "fs/path 导入",
    """import { createUserMessage } from '@deepseek-ai/dsh-llm';
import { realpath } from 'node:fs/promises';""",
    """import { createUserMessage } from '@deepseek-ai/dsh-llm';
import { realpath } from 'node:fs/promises';
// 本项目定制：appendRecentChat 往 workspace/memory/recent-chat.md 滚动归档原话。
import { appendFileSync, existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';""",
    "appendRecentChat 往 workspace/memory/recent-chat.md",
)

# ══════════════════════════════════════════════════════════════════════
# 8. connection.js —— onTypingNotice 通道
# ══════════════════════════════════════════════════════════════════════
patch(
    "connection.js",
    "onTypingNotice 字段",
    """    onNotice = () => undefined;""",
    """    onNotice = () => undefined;
    /** 本项目定制：打字状态通道。与 onNotice 分开挂，避免 poke 的节流/过滤
     *  把 input_status 顺手吞掉（官方 0.10.0 的 onNotice 只放行 poke）。 */
    onTypingNotice = () => undefined;""",
    "onTypingNotice = () => undefined;",
)

patch(
    "connection.js",
    "notice 门放行 input_status",
    """                if (noticeType === 'poke' || (noticeType === 'notify' && subType === 'poke')) {
                    try {
                        this.onNotice(event);
                    }
                    catch (error) {
                        this.log('error', 'onNotice handler failed: ' + describeError(error) + errorStack(error));
                    }
                }
                return;""",
    """                if (noticeType === 'poke' || (noticeType === 'notify' && subType === 'poke')) {
                    try {
                        this.onNotice(event);
                    }
                    catch (error) {
                        this.log('error', 'onNotice handler failed: ' + describeError(error) + errorStack(error));
                    }
                    return;
                }
                // 本项目定制（2026-11-14）：打字状态通道。
                //
                // 官方 0.10.0 的 notice 门只放行 poke，input_status 会被直接丢弃，
                // 所以这里单开一条通道交给 bridge.onTypingNotice。真实结构见
                // NapCat 源码 napcat.mjs 的 onInputStatusPush：
                //   { post_type:'notice', notice_type:'notify',
                //     sub_type:'input_status', user_id, event_type:1,
                //     status_text:'对方正在输入...' }   ← 空串 = 停止打字
                //
                // 只认 sub_type = input_status，其余 notice 一律忽略（v1 行为）。
                // 失败只记 debug：打字状态是纯增强，绝不能影响消息主链路。
                if (subType === 'input_status') {
                    try {
                        this.onTypingNotice(event);
                    }
                    catch (error) {
                        this.log('debug', 'onTypingNotice handler failed: ' + describeError(error));
                    }
                }
                return;""",
    "if (subType === 'input_status') {",
)

# ══════════════════════════════════════════════════════════════════════
# 9. index.js —— 接线 + 配置项
# ══════════════════════════════════════════════════════════════════════
patch(
    "index.js",
    "connection.onTypingNotice 接线",
    """    connection.onNotice = createPokeHandler({
        pokeEnabled: config.pokeEnabled,
        selfId: () => connection.selfId,
        dispatch: event => {
            void bridge.handleInbound(event);
        },
        log,
    });""",
    """    connection.onNotice = createPokeHandler({
        pokeEnabled: config.pokeEnabled,
        selfId: () => connection.selfId,
        dispatch: event => {
            void bridge.handleInbound(event);
        },
        log,
    });
    // 本项目定制（2026-11-14，2026-11-15 合并进官方架构）：打字状态通道。
    //
    // 与上面的 poke 通道**彻底分开**：官方 createPokeHandler 只处理 poke，
    // 把它挂在 onNotice 上，input_status 永远到不了 bridge。所以这里用
    // connection.onTypingNotice 这条独立通道（见 connection.js 的 notice 门）。
    // bridge.onTypingNotice 内部已 try/catch，任何异常都不会影响消息主链路。
    connection.onTypingNotice = (event) => {
        bridge.onTypingNotice(event);
    };""",
    "connection.onTypingNotice = (event) => {",
)

patch(
    "index.js",
    "打字感知配置项 schema",
    """    dmPolicy: z.union([z.const('open'), z.const('allowlist'), z.const('disabled')]).default('open')""",
    """    // ── 本项目定制：打字感知防抖（2026-11-14 起，11-15 合并进官方调度器）──
    typingAwareDebounce: z.boolean().default(true)
        .description('打字感知防抖（本项目定制）：开启后接入 NapCat 的「对方正在输入」notice，他打字期间绝不派发消息，停止打字后再等 inboundQuietMs 才一次性派发。需要 inboxQueueEnabled 同时开启（调度器未启用则本项无效）。关闭后退回上游的纯合并窗口行为'),
    inboundQuietMs: z.number().default(6500)
        .description('打字感知：对方停止打字后等多久（毫秒）派发。⚠️ 必须大于 NapCat 的「正在输入」推送间隔（实测 5 秒），否则下一个打字信号还没到他已经被回复了。默认 6500 = 5 秒推送周期 + 1.5 秒余量'),
    inboundTypingMaxMs: z.number().default(45000)
        .description('打字感知：绝对上限（毫秒）。以「最后一次活动」（消息或打字信号）为基准，即使他持续打字超过这个时长也强制派发一次，防止永远不回'),
    dmPolicy: z.union([z.const('open'), z.const('allowlist'), z.const('disabled')]).default('open')""",
    "typingAwareDebounce: z.boolean().default(true)",
)

patch(
    "index.js",
    "打字感知配置项透传",
    """            inboxQueueEnabled: config.inboxQueueEnabled,
            inboxQueueDelayMs: config.inboxQueueDelayMs,""",
    """            inboxQueueEnabled: config.inboxQueueEnabled,
            inboxQueueDelayMs: config.inboxQueueDelayMs,
            // 本项目定制：打字感知防抖的三项参数（bridge 构造时注入调度器）。
            typingAwareDebounce: config.typingAwareDebounce,
            inboundQuietMs: config.inboundQuietMs,
            inboundTypingMaxMs: config.inboundTypingMaxMs,""",
    "打字感知防抖的三项参数（bridge 构造时注入调度器）",
)

# ══════════════════════════════════════════════════════════════════════
# 10. inbound.js —— face/mface/图片文件名
# ══════════════════════════════════════════════════════════════════════
patch(
    "inbound.js",
    "转发节点表情改走官方 faceToEmoji",
    """            if (s?.type === 'face')
                return '😀';
            if (s?.type === 'image') {
                media.push({
                    kind: 'image',
                    url: typeof s.data?.url === 'string' && s.data.url !== '' ? s.data.url : undefined,
                    file: typeof s.data?.file === 'string' && s.data.file !== '' ? s.data.file : undefined,
                    name: ownerName,
                });
                return '[图片]';
            }""",
    """            if (s?.type === 'face') {
                // 本项目定制：转用官方 cq.js 的 faceToEmoji，让转发节点里的表情
                // 也跟随 faceStyle 配置（官方此处硬编码 '😀'，把表情信息全丢了，
                // 而且和主消息的呈现方式不一致）。
                const fid = s.data?.id !== undefined ? String(s.data.id) : '';
                return faceToEmoji(fid);
            }
            if (s?.type === 'mface') {
                // 本项目定制：官方主消息链路（cq.js parseMessage）认识 mface（表情包），
                // 但转发节点这条支路漏了，会落到 '[非文本]'。补上，保持一致。
                const mname = s.data?.summary ?? s.data?.name ?? '';
                return mname !== '' ? '[表情包:' + String(mname) + ']' : '[表情包]';
            }
            if (s?.type === 'image') {
                media.push({
                    kind: 'image',
                    url: typeof s.data?.url === 'string' && s.data.url !== '' ? s.data.url : undefined,
                    file: typeof s.data?.file === 'string' && s.data.file !== '' ? s.data.file : undefined,
                    name: ownerName,
                });
                // 本项目定制：带上文件名，模型才能分辨「哪张图是哪张」
                //（官方统一返回 '[图片]'，连拍/多图时完全无法区分）。
                const fn = typeof s.data?.file === 'string' && s.data.file !== ''
                    ? s.data.file.split(/[\\\\/]/).pop()
                    : '';
                return fn !== '' ? '[图片:' + fn + ']' : '[图片]';
            }""",
    "转发节点里的表情",
)

patch(
    "inbound.js",
    "faceToEmoji 导入",
    """import { cqUnescape, detectExplicitMention, detectMention, detectWakeName, parseMessage, segmentText, stripLeadingBotMention } from './cq.js';""",
    """import { cqUnescape, detectExplicitMention, detectMention, detectWakeName, faceToEmoji, parseMessage, segmentText, stripLeadingBotMention } from './cq.js';""",
    "detectWakeName, faceToEmoji,",
)

# ══════════════════════════════════════════════════════════════════════
# 11. outbound.js —— 拟人发送 + 防风控去重
# ══════════════════════════════════════════════════════════════════════
patch(
    "outbound.js",
    "拟人发送与去重成员",
    """    constructor(ctx, options = {}) {
        this.ctx = ctx;
        // Lazy default: read Date.now through a wrapper so an injected clock is
        // optional and callers without one keep the dynamic global lookup (fake
        // timers in tests must keep influencing the wall-clock path).
        this.now = options.now ?? (() => Date.now());
    }""",
    """    constructor(ctx, options = {}) {
        this.ctx = ctx;
        // Lazy default: read Date.now through a wrapper so an injected clock is
        // optional and callers without one keep the dynamic global lookup (fake
        // timers in tests must keep influencing the wall-clock path).
        this.now = options.now ?? (() => Date.now());
    }
    // ══════════════════════════════════════════════════════════════════
    // 本项目定制（2026-11-14）：拟人发送 + 防风控去重
    // 以下四个成员都不改变上游任何既有行为，只在发送路径上被调用。
    // ══════════════════════════════════════════════════════════════════
    /** 防风控：每聊天的近期发送记录（内容 + 时间戳），用于相似度去重。 */
    sentHistory = new Map();
    /** 去重窗口（毫秒）：10 分钟。 */
    static SENT_WINDOW_MS = 10 * 60 * 1000;
    /** 两段文本是否「高度相似」（中文 2-gram Jaccard，阈值 0.85）。 */
    static similar(a, b) {
        const norm = s => s.replace(/[\\s\\p{P}\\p{S}]/gu, '');
        const x = norm(a), y = norm(b);
        if (x === '' || y === '')
            return false;
        if (x === y)
            return true;
        const grams = s => {
            const g = new Set();
            for (let i = 0; i < s.length - 1; i++)
                g.add(s.slice(i, i + 2));
            return g;
        };
        const gx = grams(x), gy = grams(y);
        if (gx.size === 0 || gy.size === 0)
            return false;
        let inter = 0;
        for (const g of gx)
            if (gy.has(g))
                inter++;
        const union = gx.size + gy.size - inter;
        return union > 0 && inter / union >= 0.85;
    }
    /** 该聊天 10 分钟内是否发过与 text 高度相似的内容。 */
    recentlySentSimilar(chatId, text) {
        const list = this.sentHistory.get(chatId);
        if (list === undefined)
            return false;
        const cutoff = this.now() - OutboundPipeline.SENT_WINDOW_MS;
        const alive = list.filter(e => e.at >= cutoff);
        this.sentHistory.set(chatId, alive);
        return alive.some(e => OutboundPipeline.similar(e.text, text));
    }
    /** 记录本次真正发出的段落。 */
    rememberSent(chatId, paras) {
        if (paras.length === 0)
            return;
        const at = this.now();
        const list = this.sentHistory.get(chatId) ?? [];
        for (const p of paras)
            list.push({ text: p, at });
        const cutoff = at - OutboundPipeline.SENT_WINDOW_MS;
        this.sentHistory.set(chatId, list.filter(e => e.at >= cutoff));
    }
    /**
     * 本项目定制：拟人发送延迟 —— **完全由本条字数决定**。
     *
     * 真人打字速度约 200–320 字/分钟（中文），折算 190–300ms/字；
     * 但她在思考、删改、停顿，所以实际观感更慢。这里取 **260–420ms/字**，
     * 即「10 字的消息大约要 2.6–4.2 秒才敲完」——符合手机打字的真实手感。
     *
     * 反应时间：
     *   · 首条：在打字时间外额外加 700–1600ms（看到消息 → 想好怎么回）
     *   · 续条：只额外加 120–320ms（已经在聊天状态，几乎是连着敲）
     *
     * 所有部分都加 ±20% 抖动，避免恒定间隔形成可检测的节奏指纹。
     * 上下限只用于极端保护：最少 1.6s（再短就不像真人打字），最多 9s。
     *
     * @param text 本条正文
     * @param isFirst 是否为这条回复的第一条
     */
    humanDelay(text, isFirst) {
        const chars = [...text].length;
        // 打字：每字 260–420ms（按字数线性，短句短等、长句长等）
        const typing = chars * (260 + Math.random() * 160);
        // 反应：首条更久（要读消息、要想）；续条很短
        const react = isFirst
            ? 700 + Math.random() * 900    // 700–1600ms
            : 120 + Math.random() * 200;   // 120–320ms
        const base = typing + react;
        // ±20% 抖动
        const jitter = 0.8 + Math.random() * 0.4;
        // 极端保护：单条 1.6–9s
        return Math.min(9000, Math.max(1600, base * jitter));
    }""",
    "本项目定制（2026-11-14）：拟人发送 + 防风控去重",
)

# ══════════════════════════════════════════════════════════════════════
# 12. outbound.js —— 文本分支改造（分条 + 延迟 + no_reply）
# ══════════════════════════════════════════════════════════════════════
patch(
    "outbound.js",
    "文本分支：分条+延迟+no_reply",
    """                if (plain !== '') {
                    // T2 (PR port): 出站防御第三闸——正文里的 [[at:QQ]] 换成真 at 段
                    //（防御开关内；关闭时保持上游的单 text 段）。
                    const segments = defense ? splitAtSegments(plain) : [{ type: 'text', data: { text: plain } }];
                    const id = await this.sendMsg(chatId, segments, { ...options, replyTo });
                    if (id !== undefined)
                        ids.push(id);
                }""",
    """                if (plain !== '') {
                    // 本项目定制：[[qq_no_reply]] 标记 = 她只想表示「听到了」，不打算接话。
                    // 把标记从正文里剥掉；如果剥完全空、或者只剩极短一句，就按标记处理。
                    const NO_REPLY_TAG = '[[qq_no_reply]]';
                    const wantNoReply = plain.includes(NO_REPLY_TAG);
                    const cleaned = plain.split(NO_REPLY_TAG).join('').trim();
                    if (wantNoReply) {
                        // 有剩余文字就发那一句（表示「嗯，听见了」）；没剩就什么都不发。
                        if (cleaned === '') {
                            this.ctx.log('info', '[[qq_no_reply]]：不回复（' + chatId + '）');
                            this.ctx.trace?.(chatId, { stage: 'outbound', ok: true, reason: '模型选择不回复' });
                            return ids;
                        }
                        this.ctx.log('info', '[[qq_no_reply]]：只发短确认 ' + JSON.stringify(cleaned.slice(0, 30)));
                        const waitMs = this.humanDelay(cleaned, true);
                        await new Promise(resolve => setTimeout(resolve, waitMs));
                        const segs = defense ? splitAtSegments(cleaned) : [{ type: 'text', data: { text: cleaned } }];
                        const id = await this.sendMsg(chatId, segs, { ...options, replyTo });
                        if (id !== undefined)
                            ids.push(id);
                        this.rememberSent(chatId, [cleaned]);
                        this.ctx.trace?.(chatId, { stage: 'outbound', ok: true, reason: '短确认（no_reply）' });
                        return ids;
                    }
                    // 本项目定制：正文拆成多条气泡，模拟真人连发。
                    // 模型分段符不稳定（\\n 和 \\n\\n 混用），两种都拆。
                    const paras = cleaned.split(/\\n+/).map(s => s.trim()).filter(s => s !== '');
                    // 防风控：内容去重——10 分钟内已发过高度相似的句子就跳过。
                    const fresh = [];
                    for (const p of paras) {
                        if (this.recentlySentSimilar(chatId, p)) {
                            this.ctx.log('info', '防风控跳过重复内容: ' + JSON.stringify(p.slice(0, 40)));
                            continue;
                        }
                        fresh.push(p);
                    }
                    this.rememberSent(chatId, fresh);
                    const outParas = fresh.length > 0 ? fresh : [paras[paras.length - 1]];
                    // 本项目定制：每一条发出前都先等一个「拟人延迟」。
                    // 真人节奏是「看到消息 → 反应一下 → 按字数打字 → 发送」，
                    // 不是「生成完瞬间刷出」——后者是最明显的机器特征之一。
                    for (let i = 0; i < outParas.length; i++) {
                        const para = outParas[i];
                        const waitMs = this.humanDelay(para, i === 0);
                        this.ctx.log('info', '[delay-test] 第' + (i + 1) + '/' + outParas.length
                            + ' 条等待 ' + Math.round(waitMs) + 'ms  「' + para.slice(0, 20) + '」');
                        await new Promise(resolve => setTimeout(resolve, waitMs));
                        const segs = defense ? splitAtSegments(para) : [{ type: 'text', data: { text: para } }];
                        const id = await this.sendMsg(chatId, segs, { ...options, replyTo });
                        this.ctx.log('info', '[delay-test] 第' + (i + 1) + ' 条已送达 id=' + id);
                        if (id !== undefined)
                            ids.push(id);
                    }
                }""",
    "正文拆成多条气泡，模拟真人连发",
)

print("=" * 62)
for line in applied:
    print("  " + line)
print("=" * 62)
if errors:
    print("失败 %d 项：" % len(errors))
    for e in errors:
        print("  ✗ " + e)
    sys.exit(1)
print("全部补丁点就绪（%d 项）" % len(applied))

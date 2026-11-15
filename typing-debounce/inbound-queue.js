/**
 * 入站调度：排队 + 连发合并 + **打字感知防抖**。
 *
 * 前两部分来自官方 0.10.0（T7 PR port，原 lib/local/inbound-queue.js）：
 *   串序 —— 每个会话一条串行链，带图那条要下载解析比纯文字慢，原来会
 *           「先发图、再发文字」→ 先答后一句、再答前一句。现在串起来了。
 *   碎   —— 连发三条短消息原来是三个回合三句回复，理解不成一整句话。
 *           现在同会话紧挨着的几条并成一条投递。
 *
 * 第三部分是本项目定制（bot · 打字感知防抖），2026-11-14 起做、11-15 三次
 * 修正后合并回官方调度器。需求原话：
 *   「当检查到我在打字时就先不把我的消息传送给ai，
 *     等到我停止打字一段时间后再把我的消息统一传输给ai让她回复」
 *   「重要的应该是我发了一条或多条消息后仍在打字时不能回我，
 *     而不是只检测我当前的打字状态」
 *
 * **判据只有一条**（见 nextWait）：安静窗口从「最后一次打字信号」起算。
 *   - 他还在打字 → 每 5 秒来的新信号不断把窗口推后 → 永不派发
 *   - 他停了     → 窗口自然到期 → 派发一次
 *   - 没有打字能力（群聊/旧客户端）→ 用短窗口，不白等
 *
 * ⚠️ 三个实测出来的时序陷阱，改代码前务必读懂（2026-11-15 血泪）：
 *
 * ① **NapCat 推「正在输入」的间隔是 5 秒**（实测 12:05:03→08→13→15→20→35）。
 *    所以任何 < 5000ms 的静默窗口都不可靠：他还在打字，但下一个信号还没到，
 *    她就已经开口了。这就是 11-15 12:05 那次故障（gap=3519ms 正好等于旧默认值）。
 *    默认 quietMs 因此定为 6500（5 秒推送周期 + 1.5 秒余量）。
 *
 * ② **按发送键会先清空输入框** —— QQ 先推「停止输入」，消息**之后**才到。
 *    所以「停止输入」信号本身绝不能触发投递，它总是在消息之前。
 *    派发仍由「消息到达 + 安静窗口到期」驱动。
 *
 * ③ **新消息到达不能清掉打字态**，也**不能**在 flush 后删掉它。
 *    旧逻辑「收到消息 = 他不在打字」是错的：他完全可能发完一句就继续打下一句。
 *    删掉 firstSignalAt 会让下一轮退回 3500ms 短窗口，而下一个打字信号最多
 *    还要 5 秒才到 —— 3500 < 5000，又一次提前抢答。
 *
 * 开关：`typingAwareDebounce`（默认开）关闭时，本模块行为与上游逐字节等价。
 * @module dsh-onebot/inbound-queue
 */
/** 全局待处理上限：超过就丢一条并记 warn。 */
export const INBOUND_MAX_PENDING = 50;
/** 连发合并窗口——最后一条进来后等这么久（仅打字感知关闭时使用）。 */
export const INBOUND_MERGE_WINDOW_MS = 600;
/** 从第一条算起最多等这么久，超了不再等（仅打字感知关闭时使用）。 */
export const INBOUND_MERGE_MAX_WAIT_MS = 3000;
/** 打字感知：安静多久判定「他说完了」（必须 > NapCat 的 5 秒推送间隔）。 */
export const INBOUND_TYPING_QUIET_MS = 6500;
/** 打字感知：从没出现过 typing 信号时的短窗口——别让不支持的客户端白等。 */
export const INBOUND_PLAIN_QUIET_MS = 3500;
/** 打字感知：距今多久没有新信号就认为那批打字信号已经断了（用于锚点刷新）。 */
export const INBOUND_TYPING_STALE_MS = 8000;
/** 打字感知：有打字历史时的绝对上限，防止永远不回。 */
export const INBOUND_TYPING_MAX_MS = 45000;
/** 打字感知：没有打字历史时的绝对上限。 */
export const INBOUND_PLAIN_MAX_MS = 12000;
/**
 * 把同一会话里紧挨着的几条并成一条：段的顺序保持原样，id/时间取最后一条
 * （T7 口径：合并后的合成消息 mid = 最后一条的 message_id，与 review-t2.md
 *  的 <user_message mid=…> 语义衔接——模型 [[reply:]] 引用的是最后一句）。
 */
export function mergeInboundEvents(events) {
    const first = events[0];
    const last = events[events.length - 1];
    const message = [];
    const raws = [];
    for (const event of events) {
        if (Array.isArray(event.message))
            message.push(...event.message);
        else if (typeof event.message === 'string')
            message.push({ type: 'text', data: { text: event.message } });
        raws.push(typeof event.raw_message === 'string' ? event.raw_message : '');
    }
    return {
        ...first,
        message,
        raw_message: raws.join('\n'),
        message_id: last.message_id,
        time: last.time,
    };
}
/** 排队用的会话键。拿不到会话就归到 other，至少保证同一批 unknown 也是串行的。 */
export function inboundChatKey(event) {
    const type = event?.message_type;
    if (type === 'private')
        return 'private:' + String(event?.user_id ?? '');
    if (type === 'group')
        return 'group:' + String(event?.group_id ?? '');
    return 'other';
}
/** 斜杠命令不合并：命令要一条一条按原样执行。 */
export function isCommandText(event) {
    const segs = Array.isArray(event?.message) ? event?.message : [];
    const text = segs
        .filter(seg => seg?.type === 'text')
        .map(seg => String(seg.data?.text ?? ''))
        .join('')
        .trim();
    return text.startsWith('/');
}
export class InboundScheduler {
    log;
    deliver;
    injectDeliver;
    maxPending;
    mergeWindowMs;
    mergeMaxWaitMs;
    /** 打字感知开关（本项目定制）。关闭时全模块退回上游行为。 */
    typingAware;
    /** 安静多久派发（打字感知开启时生效）。 */
    quietMs;
    /** 有打字历史时的绝对上限。 */
    typingMaxMs;
    /** 每会话串行链：key → 链尾 promise。 */
    chains = new Map();
    /** 全局待处理计数（含合并槽里未投递的）。 */
    pending = 0;
    /** 每会话合并槽。 */
    slots = new Map();
    /**
     * 打字状态表：key → { active, stopped, lastSignalAt, firstSignalAt }。
     * 只由 noteTyping() 写入 —— 消息到达、合并、派发都不会伪造这里的值。
     */
    typingStates = new Map();
    constructor(options) {
        this.log = options.log;
        this.deliver = options.deliver;
        this.injectDeliver = options.injectDeliver ?? options.deliver;
        this.maxPending = options.maxPending ?? INBOUND_MAX_PENDING;
        this.mergeWindowMs = options.mergeWindowMs ?? INBOUND_MERGE_WINDOW_MS;
        this.mergeMaxWaitMs = options.mergeMaxWaitMs ?? INBOUND_MERGE_MAX_WAIT_MS;
        // 本项目定制：打字感知参数（缺省开启，保持本项目既有行为）。
        this.typingAware = options.typingAware !== false;
        const quiet = options.quietMs;
        this.quietMs = typeof quiet === 'number' && quiet >= 0 ? quiet : INBOUND_TYPING_QUIET_MS;
        const tmax = options.typingMaxMs;
        this.typingMaxMs = typeof tmax === 'number' && tmax > 0 ? tmax : INBOUND_TYPING_MAX_MS;
    }
    /**
     * 投一条消息进来。注入轮不排队也不合并，直接串进本会话的链里跑；
     * 其余走连发合并（斜杠命令除外，命令一条一条按原样执行）。
     */
    push(event, opts = {}) {
        if (opts.injected === true) {
            return this.enqueue(inboundChatKey(event), () => this.injectDeliver(event));
        }
        if (isCommandText(event)) {
            return this.enqueue(inboundChatKey(event), () => this.deliver(event));
        }
        const key = inboundChatKey(event);
        const now = Date.now();
        let slot = this.slots.get(key);
        if (slot === undefined) {
            slot = { events: [], waiters: [], timer: undefined, firstAt: now, lastActivityAt: now };
            this.slots.set(key, slot);
        }
        slot.events.push(event);
        // ⚠️ 陷阱③：这里**不碰** typingStates 的 active/stopped —— 新消息到达
        // 不代表他不在打字，他完全可能发完一句就继续打下一句。
        //
        // 但锚点要做一次「过期刷新」：如果上一条打字信号已经超过一个完整的
        // 过期周期（TYPING_STALE_MS），说明那批信号早就断了，此时把锚点挪到
        // 这条消息上。否则 quietMs - (now - 陈旧锚点) 会算成负数 → 立刻派发，
        // 又变回「他刚发完、正要接着打就被抢答」的老毛病。
        const st = this.typingStates.get(key);
        if (st !== undefined && now - st.lastSignalAt >= INBOUND_TYPING_STALE_MS) {
            st.lastSignalAt = now;
            st.active = false;
            st.stopped = true;
        }
        slot.lastActivityAt = now;
        const done = new Promise(resolve => {
            slot.waiters.push(resolve);
        });
        this.reschedule(key, slot);
        return done;
    }
    /**
     * 本项目定制：「对方正在输入」notice 的唯一入口（bridge 转发）。
     *
     * @param key 会话键（'private:<QQ>' / 'group:<群号>'）
     * @param isTyping 正在打字 = true；停止打字 = false
     */
    noteTyping(key, isTyping) {
        if (this.typingAware !== true)
            return;
        const now = Date.now();
        const prev = this.typingStates.get(key);
        if (isTyping) {
            this.typingStates.set(key, {
                active: true,
                stopped: false,
                lastSignalAt: now,
                firstSignalAt: prev !== undefined && prev.active === true ? (prev.firstSignalAt || now) : now,
            });
        }
        else {
            // ⚠️ 陷阱②：「停止输入」总是在消息**之前**到达（按发送键清空了输入框），
            // 所以这里只记录「候选停止」，**不触发投递**。真正派发仍由
            // 「消息到达 + 安静窗口到期」驱动 —— 但锚点挪到此刻恰好等价于
            // 「他按下发送的那一刻」，比从消息到达起算更准。
            this.typingStates.set(key, {
                active: false,
                stopped: true,
                lastSignalAt: now,
                firstSignalAt: prev !== undefined ? (prev.firstSignalAt || now) : now,
            });
        }
        const slot = this.slots.get(key);
        this.log('debug', '打字状态更新（' + (isTyping ? '正在输入' : '停止输入') + '）: ' + key
            + (slot === undefined ? '' : '，缓冲 ' + slot.events.length + ' 条'));
        if (slot === undefined)
            return; // 没有待派发的消息 → 没有可重排的计时器
        this.reschedule(key, slot);
    }
    /**
     * 重排某个会话的合并计时器：先算 nextWait()，为 0 才真的派发，
     * 否则等到点再重新评估一次（打字信号可能在这期间又来了）。
     */
    reschedule(key, slot) {
        if (slot.timer !== undefined) {
            clearTimeout(slot.timer);
            slot.timer = undefined;
        }
        const wait = this.nextWait(key, slot);
        slot.timer = setTimeout(() => this.tick(key), Math.max(0, wait));
        this.log('debug', '入站缓冲 ' + slot.events.length + ' 条，' + Math.round(wait)
            + 'ms 后派发（' + this.describeWait(key) + '）');
    }
    /** 计时器到点：重新算一遍。为 0 → 派发；否则再等下一轮。 */
    tick(key) {
        const slot = this.slots.get(key);
        if (slot === undefined)
            return;
        slot.timer = undefined;
        if (this.nextWait(key, slot) <= 0) {
            this.flush(key);
            return;
        }
        this.reschedule(key, slot);
    }    /**
     * 算出「还要等多久」。这是整个防抖的唯一判据来源，返回 0 表示可以派发。
     *
     * 打字感知关闭 → 完全照抄上游：min(mergeWindowMs, mergeMaxWaitMs - 首条至今)。
     *
     * 打字感知开启 → 一条统一公式，**锚点都是「最后一次打字信号」**：
     *     wait = quietMs - (now - lastSignalAt)
     *
     * 为什么这样就够，不需要再叠一层「信号过期」判据：
     *   · NapCat 打字时每 5 秒推一次「正在输入」，每次 noteTyping 都会重排计时器，
     *     把 wait 重新拉回 ~6500ms。所以**只要他还在打字就永远不会派发**。
     *   · 他停下来（无论是不发消息地停，还是按发送键的「停止输入」），
     *     lastSignalAt 定格，6500ms 后自然派发一次。
     *   · 陷阱②保证「停止输入」在消息之前到达，锚点正好是他按下发送的那一刻，
     *     所以实际表现是「发完再等 6.5 秒」——比从消息到达起算更准。
     *
     * 从没出现过任何 typing 信号（对方客户端不支持 / 群聊）→ 用短窗口 3500，
     * 否则纯文字场景会白等 6.5 秒。
     */
    nextWait(key, slot) {
        const now = Date.now();
        if (this.typingAware !== true) {
            // 上游口径：以「首条到达」为锚点，最多等 mergeMaxWaitMs。
            // 到点即派发（返回 0），不因 mergeWindowMs 反复推迟。
            const elapsed = now - slot.firstAt;
            const merged = this.mergeWindowMs - elapsed;
            const capped = this.mergeMaxWaitMs - elapsed;
            if (elapsed >= this.mergeMaxWaitMs)
                return 0;                       // 到上限，立即派发
            if (merged <= 0)
                return 0;                       // 合并窗口已满，立即派发
            return Math.min(merged, capped);
        }
        const st = this.typingStates.get(key);
        const sawTyping = st !== undefined && (st.firstSignalAt || 0) > 0;
        if (!sawTyping) {
            // 无打字历史：从最后一条消息起算短窗口。
            const since = now - slot.lastActivityAt;
            if (since >= INBOUND_PLAIN_MAX_MS)
                return 0;
            const left = INBOUND_PLAIN_QUIET_MS - since;
            return left <= 0 ? 0 : left;
        }
        // 有打字历史：绝对上限以「最后一次活动」起算，防止永远不回。
        if (now - slot.lastActivityAt >= this.typingMaxMs)
            return 0;
        // 安静窗口从最后一次打字信号起算（而不是从消息到达起算）：
        // 陷阱②让停止信号先于消息到，这个锚点才代表「他真正停手」的时刻。
        const left = this.quietMs - (now - st.lastSignalAt);
        return left <= 0 ? 0 : left;
    }
    /** 只用于 debug 日志：说清这次等待是怎么算出来的。 */
    describeWait(key) {
        if (this.typingAware !== true)
            return '上游模式';
        const st = this.typingStates.get(key);
        const sawTyping = st !== undefined && (st.firstSignalAt || 0) > 0;
        if (!sawTyping)
            return '无打字历史，短窗口 ' + INBOUND_PLAIN_QUIET_MS + 'ms';
        const now = Date.now();
        const since = now - st.lastSignalAt;
        return '打字锚点 ' + this.quietMs + 'ms 前（已过 ' + Math.round(since) + 'ms'
            + '，' + (st.active === true && st.stopped !== true ? '他还在打' : '他停了') + '）';
    }
    /** 把一条投进它所属会话的串行链；全局积压超过上限就丢并记 warn。 */
    enqueue(key, run) {
        if (this.pending >= this.maxPending) {
            this.log('warn', '入站队列已满(' + String(this.maxPending) + ')，丢弃一条: ' + key);
            return Promise.resolve();
        }
        this.pending += 1;
        const prev = this.chains.get(key) ?? Promise.resolve();
        const next = prev
            .catch(() => undefined)
            .then(run)
            .catch((error) => {
            const detail = error instanceof Error ? (error.stack ?? error.message) : String(error);
            this.log('error', 'inbound handling failed: ' + detail);
        })
            .finally(() => {
            this.pending -= 1;
            if (this.chains.get(key) === next)
                this.chains.delete(key);
        });
        this.chains.set(key, next);
        return next;
    }
    /** 把攒着的几条并成一条投出去，然后放行所有等着的调用方。 */
    flush(key) {
        const slot = this.slots.get(key);
        if (slot === undefined)
            return;
        this.slots.delete(key);
        if (slot.timer !== undefined)
            clearTimeout(slot.timer);
        // ⚠️ 陷阱③：这里**不能**删 typingStates。
        //
        // 删了就等于「本会话从没出现过打字信号」，下一轮会退回 3500ms 短窗口；
        // 而他若正在连续打字，下一个「正在输入」最多还要 5 秒才到 ——
        // 3500 < 5000，又变成提前抢答。这正是 11-15 那次故障的翻版。
        //
        // 正确做法：保留 firstSignalAt（记住「这个客户端会推送打字状态」），
        // 只把锚点挪到此刻，让下一轮从完整的 quietMs 重新起算。
        const st = this.typingStates.get(key);
        if (st !== undefined) {
            st.active = false;
            st.stopped = true;
            st.lastSignalAt = Date.now();
        }
        const merged = slot.events.length === 1 ? slot.events[0] : mergeInboundEvents(slot.events);
        const waiters = slot.waiters;
        slot.waiters = [];
        const task = this.enqueue(key, () => this.deliver(merged));
        void task.then(() => {
            for (const resolve of waiters)
                resolve();
        });
    }
    /** 关闭：清掉所有合并定时器并放行等待者，别在关闭途中再投递。 */
    stop() {
        for (const slot of this.slots.values()) {
            if (slot.timer !== undefined)
                clearTimeout(slot.timer);
            for (const resolve of slot.waiters)
                resolve();
        }
        this.slots.clear();
        this.typingStates.clear();
    }
}

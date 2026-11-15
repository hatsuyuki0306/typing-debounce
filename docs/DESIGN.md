# 设计说明：为什么这样实现

> 这份文档解释 `typing-debounce/inbound-queue.js` 里的每个决策。
> 想快速上手看 [README](README.md)，想理解取舍看这里。

---

## 一、为什么合并进官方调度器，而不是另写一套

动手时面对两条路：

| 方案 | 优点 | 缺点 |
| --- | --- | --- |
| A. 在 `bridge.js` 里自己写缓冲 + 定时器 | 不动官方代码 | 放弃官方已有的串行链和合并能力 |
| B. 在官方 `InboundScheduler` 上加打字感知 | 两套能力共存 | 要理解官方实现 |

**选 B。** 理由：

官方调度器解决的是**两个不同的问题**：

```
串序 —— 带图消息要下载解析，比纯文字慢。原来「先发图、再发文字」
        会变成「先答后一句，再答前一句」。现在每会话一条串行链。
碎   —— 连发三条短消息原来是三个回合三句回复。现在并成一条投递。
```

这两个问题和我关心的「打字时别抢话」是**正交**的。
另写一套意味着我要重新实现串行链和合并，还要保证两者不冲突 —— 没必要。

### 融合方式

不改动官方的任何既有方法，只**新增**：

```js
noteTyping(key, isTyping)      // 新入口
typingStates = new Map()       // 新状态表
nextWait(key, slot)            // 替换原有的等待计算
tick(key)                      // 新增：到点后重新评估
```

关键的 `push()` 只加了两处：一个「过期刷新」，一个改调 `reschedule`。

**开关关闭时行为与上游逐字节等价**：

```js
nextWait() {
    if (this.typingAware !== true) {
        // 完全照抄上游
        const elapsed = now - slot.firstAt;
        return Math.max(0, Math.min(this.mergeWindowMs, this.mergeMaxWaitMs - elapsed));
    }
    ...
}
```

这样即使方案失败，也可以一行配置退回原状。

---

## 二、为什么锚点是「最后一次打字信号」

最初我用了「消息到达」当锚点，逻辑是：消息到达 → 等 6500ms → 派发；
期间如果来打字信号就取消。

**这个模型有个致命问题**：它把「等待」和「状态」混在一起了。

用「信号到达时刻」当锚点更简单也更准：

```
wait = quietMs - (now - lastSignalAt)
```

- 他打字 → 每 5 秒新信号 → `lastSignalAt` 刷新 → wait 重置回 6500
- 他停手 → `lastSignalAt` 定格 → 6500ms 后 wait 变 0 → 派发

**一个公式覆盖所有情况**，不需要「取消定时器」「重新设置」这类状态机操作。

### 副作用：更准的锚点

坑②说「停止输入」信号先于消息到达。
这意味着 `lastSignalAt` 正好记录在**他按下发送键的那一刻**，
而消息到达要晚几百毫秒。

所以实际等待时间 = 「从他停手算起 6500ms」，
比「从消息到达算起 6500ms」**更贴近真实语义**。

---

## 三、为什么需要 `firstSignalAt`

`lastSignalAt` 记录「最近一次信号」，`firstSignalAt` 记录「这个会话有没有过信号」。

看起来冗余，但它承担一个关键判断：**该用长窗口还是短窗口**。

```js
const sawTyping = st !== undefined && (st.firstSignalAt || 0) > 0;
const quietWindow = sawTyping ? this.quietMs : INBOUND_PLAIN_QUIET_MS;
```

| 情况 | 用哪个窗口 | 为什么 |
| --- | --- | --- |
| 有打字历史 | 6500ms | 他可能会继续打，必须跨过一个 5 秒推送周期 |
| 无打字历史（群聊 / 旧客户端） | 3500ms | 不会有打字信号了，等 6.5 秒纯属白等 |

**如果没有这个区分**，纯文字场景（比如某些客户端不支持 `input_status`）
每次都要多等 3 秒，体验明显变差。

### 为什么 flush 之后不能删

这是坑③的核心。删掉 `typingStates` 就等于「这个会话从没出现过打字信号」
→ 下一轮退回 3500ms → 而下一个信号最多还要 5 秒才到 → 3500 < 5000 → 抢话。

所以 `flush()` 里做的是**保留 + 挪锚点**，不是删除：

```js
const st = this.typingStates.get(key);
if (st !== undefined) {
    st.active = false;
    st.stopped = true;
    st.lastSignalAt = Date.now();   // 只挪锚点，保留 firstSignalAt
}
```

---

## 四、为什么用 `tick` 而不是直接 `flush`

```js
slot.timer = setTimeout(() => this.tick(key), Math.max(0, wait));

tick(key) {
    if (this.nextWait(key, slot) <= 0) {
        this.flush(key);
    } else {
        this.reschedule(key, slot);   // 再等一轮
    }
}
```

原因：**定时器到点时，世界可能已经变了。**

`wait` 是在设置定时器那一刻算出来的。但在这段时间里：

- 可能来了新的打字信号（`lastSignalAt` 变了）
- 可能来了新消息（`lastActivityAt` 变了）

所以到点后必须**重新计算一遍**，而不是盲目派发。
`tick` 就是「重新评估」这个语义。

**双重保险**：即使 `reschedule` 时算错了，`tick` 也会在到点时再检查一次。

---

## 五、为什么 `push` 里要做「过期刷新」

```js
const st = this.typingStates.get(key);
if (st !== undefined && now - st.lastSignalAt >= INBOUND_TYPING_STALE_MS) {
    st.lastSignalAt = now;
    st.active = false;
    st.stopped = true;
}
```

场景：他 30 秒前打过字，然后去干别的了，现在回来发了一条消息。

- 如果不刷新：`quietMs - (now - 30秒前)` = 6500 - 30000 = **负数** → 立刻派发
- 刷新后：锚点挪到此刻 → 正常等 6500ms

**负数会被 `Math.max(0, wait)` 变成 0**，看起来"正常工作"，实际是完全失去防抖。

这类「边界值恰好被 clamp 掩盖」的 bug 最难发现，因为它不会报错。

---

## 六、为什么打字感知只服务私聊

```js
const chatId = buildChatId('private', userId);
```

群聊里 `input_status` 的语义是「有人在打字」，但 `user_id` 是谁？

- 可能是打字那个人
- 也可能在群里消息过多时被节流或合并

**无法可靠归属到具体人**，而群聊的回复决策本来就走 @ 门（只有 @ 机器人才回复），
不需要防抖。

强行支持反而会引入「某人打字导致机器人对别人的消息不回复」这种诡异行为。

---

## 七、为什么两条 notice 通道分开

官方 0.10.0 的 notice 门是这样：

```js
if (noticeType === 'poke' || (noticeType === 'notify' && subType === 'poke')) {
    this.onNotice(event);     // ← 只有 poke 能过
}
return;                       // ← input_status 在这里被丢弃
```

有两条路可选：

| 方案 | 问题 |
| --- | --- |
| 在 `onNotice` 里加 `input_status` 判断 | `onNotice` 挂的是官方 `createPokeHandler`，它的节流/过滤逻辑可能顺手把打字信号吞掉 |
| **新增 `onTypingNotice` 独立通道** | 多一个字段，但两条通道完全隔离 |

选后者。理由：**poke 是「事件」，打字状态是「状态」**，
两者的处理逻辑完全不同（事件要节流去重，状态要实时更新）。
混在一个通道里迟早出问题。

代码里两条通道互不影响：

```js
if (poke) {
    try { this.onNotice(event); } catch (e) { log('error', ...); }
    return;
}
if (subType === 'input_status') {
    try { this.onTypingNotice(event); } catch (e) { log('debug', ...); }
}
```

注意日志级别不同：poke 失败记 `error`（用户有感知），打字失败只记 `debug`
（纯增强，失败不该刷屏）。

---

## 八、为什么要有 `typingMaxMs` 上限

如果哪天 NapCat 的 `input_status` 推送出现异常（比如一直推「正在输入」），
没有上限就会**永远不回复**。

```
typingMaxMs = 45000   // 以「最后一次活动」为基准
```

注意基准是**最后一次活动**（消息或打字信号都算），不是「第一条消息」。
用「第一条消息」当基准会导致他持续打字 45 秒后被强行打断 —— 那正是要避免的。

---

## 九、测试为什么用假时钟

```js
function makeClock() {
    let now = 1_000_000;
    const timers = new Map();
    return {
        now: () => now,
        setTimeout: (fn, ms) => { ... },
        advance: async (ms) => { ... },
    };
}
```

真实时间下测「等 6500ms 不派发」需要真的等 6.5 秒，
31 个用例里有十几个这样的场景，测试要跑几分钟，没人会愿意跑。

假时钟让整个测试套件在 **1 秒内**跑完，才能真正做到「每次改动都跑」。

### 一个踩过的坑：`advance` 必须是 async

调度器内部用 Promise 链做串行化（`enqueue`）。假时钟同步推进时，
微任务还没执行完，观察不到 `delivered` 的变化，测试会假失败。

所以 `advance` 最后要 `await` 一下让出事件循环：

```js
now = target;
await new Promise(r => REAL_SET_TIMEOUT(r, 0));
```

### 另一个坑：先推进时间，再送信号

```js
// ✅ 正确顺序
await s.clock.advance(2000);
s.q.noteTyping(KEY, true);

// ❌ 错误顺序（会让计时器在「已经推进到未来」的时钟上算出过期时刻）
s.q.noteTyping(KEY, true);
await s.clock.advance(2000);
```

这个错误一开始让我误判成代码有 bug，实际是测试自己写错了。
**测试的时序要和真实世界一致**：先过时间，再有事件。

---

## 十、为什么不用「信号过期」判据

我最初的设计里有这么一条：

```js
const typingLive = st.active && !st.stopped && (now - st.lastSignalAt) < STALE_MS;
if (typingLive) return;  // 他在打字，不派发
```

后来删掉了。原因：**它和「安静窗口」是同一件事的两种表达。**

`STALE_MS = 8000` 而 `quietMs = 6500`。
如果 `(now - lastSignalAt) < 6500`，那必然 `< 8000`，
所以「窗口未满」已经蕴含了「信号新鲜」。

两层判据除了让代码更复杂、让测试更难测之外，没有带来任何额外保护。

> **删掉冗余判据**比加判据更需要判断力。多余的条件会让边界情况难以推理。

---

## 十一、遗留的设计取舍

| 取舍 | 说明 |
| --- | --- |
| 不看「打字速度」 | 只判断在不在打字，不估算还要打多久。后者不可靠且容易误判 |
| 不做「消息内容预测」 | 有些人键入很快但字数少，有些相反。按内容判断的收益不抵复杂度 |
| 不区分「停顿」和「结束」 | 去倒杯水回来继续打，会被当成新会话。这是可接受的误判（最坏就是早回 6.5 秒） |
| 窗口是全局常量 | 不做「按用户习惯自适应」。数据不足时自适应就是过拟合 |

---

## 附：完整调用链

```
NapCat 推 notice(input_status)
    ↓
connection.js  notice 门：subType === 'input_status'
    ↓
index.js       connection.onTypingNotice = ev => bridge.onTypingNotice(ev)
    ↓
bridge.js      onTypingNotice()
               ├─ 校验 post_type / sub_type / user_id
               ├─ 只处理私聊
               └─ 解析 isTyping = status_text 非空 || event_type === 1
    ↓
inbound-queue  noteTyping(chatId, isTyping)
               ├─ 更新 typingStates（active / stopped / lastSignalAt / firstSignalAt）
               └─ 若有缓冲槽 → reschedule
    ↓
reschedule     wait = nextWait()  → setTimeout(tick, wait)
    ↓
tick           nextWait() <= 0 ? flush() : reschedule()
    ↓
flush          合并缓冲的消息 → deliver() → 落进官方串行链
```

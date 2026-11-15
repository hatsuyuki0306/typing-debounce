// ⚠️ 本文件由项目脚本自动脱敏生成：本机路径 → <PLUGIN_DIR>/<PROJECT_DIR>，
//    真实 QQ 号 → 10000001/10000002。逻辑与原版逐字节相同，仅替换了标识。
/**
 * 打字感知防抖 —— 针对 0.10.0 官方 InboundScheduler 架构的真实逻辑单测。
 *
 * 原则（项目铁律）：**import 真实模块**，绝不把逻辑抄一遍。
 * 旧版 test-typing.mjs 用 extractMethod 从 bridge.js 抠方法体，那是 0.9.7
 * 的架构；0.10.0 已把防抖搬进 InboundScheduler，所以本文件直接 import。
 *
 * 三条实测时序陷阱（每个都有对应用例）：
 *   ① NapCat 每 5 秒才推一次「正在输入」→ 任何 < 5000ms 的窗口都不可靠
 *   ② 按发送键先推「停止输入」，消息之后才到 → 停止信号不能直接触发派发
 *   ③ 新消息到达 / flush 之后都不能清掉打字历史 → 否则退回短窗口又抢答
 *
 * 跑法：node typing-debounce/test-inbound-queue.mjs
 */
import {
  InboundScheduler,
  INBOUND_TYPING_QUIET_MS,
  INBOUND_PLAIN_QUIET_MS,
} from './inbound-queue.js';

let pass = 0, fail = 0;
const failures = [];

function ok(name, cond, detail) {
  if (cond) { pass++; console.log('  PASS  ' + name); }
  else { fail++; failures.push(name + (detail ? ' — ' + detail : '')); console.log('  FAIL  ' + name + (detail ? ' — ' + detail : '')); }
}

/** 真实 setTimeout —— 被接管后仍需要它让出事件循环，清空微任务链。 */
const REAL_SET_TIMEOUT = global.setTimeout.bind(global);

/** 假时钟：接管 setTimeout/clearTimeout/Date.now，可精确推进。
 *  advance 是 async 的：调度器内部用 Promise 链串行化投递，必须把微任务
 *  跑完才能观察到 delivered 的变化。 */
function makeClock() {
  let now = 1_000_000;
  let seq = 1;
  const timers = new Map();
  const clock = {
    now: () => now,
    setTimeout: (fn, ms) => { const id = seq++; timers.set(id, { at: now + Math.max(0, ms), fn }); return id; },
    clearTimeout: (id) => { timers.delete(id); },
    /** 推进 ms，期间到点的 timer 按时间顺序执行；末尾清空微任务。 */
    advance: async (ms) => {
      const target = now + ms;
      for (;;) {
        let best = null, bestId = null;
        for (const [id, t] of timers) {
          if (t.at <= target && (best === null || t.at < best.at)) { best = t; bestId = id; }
        }
        if (best === null) break;
        timers.delete(bestId);
        now = best.at;
        best.fn();
      }
      now = target;
      // 让出事件循环，让调度器的 Promise 链（enqueue → deliver）跑完
      await new Promise(r => REAL_SET_TIMEOUT(r, 0));
    },
    pending: () => timers.size,
  };
  return clock;
}

/** 建一个受控调度器：真实 InboundScheduler + 假时钟 + 记录投递。 */
function makeScheduler(opts = {}) {
  const clock = makeClock();
  const delivered = [];
  const realSetTimeout = global.setTimeout;
  const realClearTimeout = global.clearTimeout;
  const realDateNow = Date.now;

  global.setTimeout = clock.setTimeout;
  global.clearTimeout = clock.clearTimeout;
  Date.now = clock.now;

  const q = new InboundScheduler({
    log: () => {},
    deliver: (ev) => { delivered.push({ at: clock.now(), ev }); return Promise.resolve(); },
    typingAware: opts.typingAware !== false,
    ...(opts.quietMs !== undefined ? { quietMs: opts.quietMs } : {}),
    ...(opts.typingMaxMs !== undefined ? { typingMaxMs: opts.typingMaxMs } : {}),
  });

  return {
    clock, q, delivered,
    restore: () => { global.setTimeout = realSetTimeout; global.clearTimeout = realClearTimeout; Date.now = realDateNow; },
  };
}

const KEY = 'private:10000001';
const msg = (id, text) => ({
  post_type: 'message', message_type: 'private', user_id: '10000001',
  message_id: id, time: 1, raw_message: text,
  message: [{ type: 'text', data: { text } }],
  sender: { user_id: '10000001', nickname: 'user' },
});

console.log('══════ 常量自检 ══════');
ok('quietMs 默认 6500', INBOUND_TYPING_QUIET_MS === 6500, '实际 ' + INBOUND_TYPING_QUIET_MS);
ok('无打字历史短窗口 3500', INBOUND_PLAIN_QUIET_MS === 3500, '实际 ' + INBOUND_PLAIN_QUIET_MS);
ok('安静窗口 > NapCat 5 秒推送间隔', INBOUND_TYPING_QUIET_MS > 5000);

console.log('\n══════ 基线：纯文字（无打字能力）══════');
{
  const s = makeScheduler();
  s.q.push(msg(1, '在吗'));
  ok('立即不派发', s.delivered.length === 0);
  await s.clock.advance(3400);
  ok('3400ms 仍不派发', s.delivered.length === 0);
  await s.clock.advance(200);
  ok('3600ms 后派发', s.delivered.length === 1, '实际 ' + s.delivered.length);
  s.restore();
}

console.log('\n══════ 陷阱①：他还在打字时绝不派发 ══════');
{
  const s = makeScheduler();
  // 先在打字（有打字历史），消息才到 —— 这是真实顺序：
  // 他肯定先动手指打了一阵，才会发出一条消息。
  s.q.noteTyping(KEY, true);
  s.q.push(msg(1, '写作业写的有点累了'));
  // NapCat 每 5 秒推一次「正在输入」
  for (let i = 0; i < 6; i++) {
    await s.clock.advance(5000);
    s.q.noteTyping(KEY, true);
    ok('第 ' + (i + 1) + ' 次打字信号后仍未派发', s.delivered.length === 0, '实际派发 ' + s.delivered.length);
  }
  // 停止打字 → 再等完整安静窗口
  s.q.noteTyping(KEY, false);
  await s.clock.advance(INBOUND_TYPING_QUIET_MS - 100);
  ok('停止后未满安静窗口不派发', s.delivered.length === 0);
  await s.clock.advance(200);
  ok('停止后满安静窗口派发一次', s.delivered.length === 1, '实际 ' + s.delivered.length);
  s.restore();
}

console.log('\n══════ 【核心】发完一条继续打字，不能抢答 ══════');
{
  const s = makeScheduler();
  // 真实顺序：他已经打了一会儿字 → 发出第一条
  s.q.noteTyping(KEY, true);
  await s.clock.advance(3000);
  s.q.push(msg(1, '今天好累'));
  // ⚠️ 顺序要紧：必须先推进时间、再送打字信号。
  // 反过来（先 noteTyping 再 advance）会让计时器在「已经推进到未来」的
  // 假时钟上算出一个过期时刻，测试自己制造假失败。
  await s.clock.advance(2000);
  s.q.noteTyping(KEY, true);
  // 又发了一条，同时还在打字
  await s.clock.advance(1000);
  s.q.push(msg(2, '但还行'));
  ok('发第二条后不立刻派发', s.delivered.length === 0, '实际 ' + s.delivered.length);
  // 继续打字 4 个周期（共 20 秒）—— 全程不能派发
  for (let i = 0; i < 4; i++) {
    await s.clock.advance(4000);
    s.q.noteTyping(KEY, true);
  }
  ok('持续打字 20 秒仍未派发', s.delivered.length === 0, '实际 ' + s.delivered.length);
  // 真正停止
  s.q.noteTyping(KEY, false);
  await s.clock.advance(INBOUND_TYPING_QUIET_MS + 100);
  ok('停止后派发一次', s.delivered.length === 1, '实际 ' + s.delivered.length);
  ok('派发的是合并后的两条',
    s.delivered.length === 1 && s.delivered[0].ev.message.filter(x => x.type === 'text').length === 2,
    s.delivered.length === 0 ? '没派发' : '实际 ' + s.delivered[0].ev.message.length + ' 段');
  s.restore();
}

console.log('\n══════ 陷阱②：停止信号先于消息到达 ══════');
{
  const s = makeScheduler();
  // 他打字
  s.q.noteTyping(KEY, true);
  await s.clock.advance(5000);
  // 按发送键 → 先来「停止输入」
  s.q.noteTyping(KEY, false);
  // 消息随后才到（同一秒内）
  await s.clock.advance(300);
  s.q.push(msg(1, '我去吃饭了'));
  ok('消息到达后不立即派发', s.delivered.length === 0);
  await s.clock.advance(INBOUND_TYPING_QUIET_MS - 500);
  ok('安静窗口未满不派发', s.delivered.length === 0);
  await s.clock.advance(400);
  ok('安静窗口满派发', s.delivered.length === 1, '实际 ' + s.delivered.length);
  s.restore();
}

console.log('\n══════ 陷阱③：flush 后不能丢打字历史 ══════');
{
  const s = makeScheduler();
  // 第一轮：打字 → 停止 → 派发
  s.q.noteTyping(KEY, true);
  await s.clock.advance(1000);
  s.q.push(msg(1, '第一次'));
  s.q.noteTyping(KEY, false);
  await s.clock.advance(INBOUND_TYPING_QUIET_MS + 100);
  ok('第一轮已派发', s.delivered.length === 1, '实际 ' + s.delivered.length);
  // 第二轮：如果历史被丢了，会退回 3500ms 短窗口 → 抢答
  await s.clock.advance(1000);
  s.q.push(msg(2, '第二次'));
  s.q.noteTyping(KEY, true);           // 他又开始打字
  await s.clock.advance(4000);
  ok('第二轮打字中 4 秒不派发（若丢历史会在此抢答）', s.delivered.length === 1,
    '实际 ' + s.delivered.length);
  s.q.noteTyping(KEY, false);
  await s.clock.advance(INBOUND_TYPING_QUIET_MS + 100);
  ok('第二轮正常派发', s.delivered.length === 2, '实际 ' + s.delivered.length);
  s.restore();
}

console.log('\n══════ 绝对上限：持续打字也不会永远不回 ══════');
{
  const s = makeScheduler({ typingMaxMs: 20000 });
  s.q.push(msg(1, '一直打一直打'));
  s.q.noteTyping(KEY, true);
  let dispatched = false;
  for (let i = 0; i < 10; i++) {
    await s.clock.advance(5000);
    s.q.noteTyping(KEY, true);
    if (s.delivered.length > 0) { dispatched = true; break; }
  }
  ok('超过 typingMaxMs 后强制派发一次', dispatched && s.delivered.length === 1,
    '实际 ' + s.delivered.length);
  s.restore();
}

console.log('\n══════ 关闭打字感知 → 上游行为逐字节等价 ══════');
{
  const s = makeScheduler({ typingAware: false });
  s.q.noteTyping(KEY, true);
  s.q.push(msg(1, '测试'));
  ok('关闭时打字信号被忽略，立即开计时', s.delivered.length === 0);
  await s.clock.advance(650);   // 上游 mergeWindowMs 默认 600
  ok('600ms 后派发（上游合并窗口）', s.delivered.length === 1, '实际 ' + s.delivered.length);
  s.restore();
}

console.log('\n══════ 命令不合并、按原样执行 ══════');
{
  const s = makeScheduler();
  s.q.noteTyping(KEY, true);
  s.q.push(msg(1, '/new'));
  // 命令走 enqueue 串行链（Promise），要让出事件循环才观察得到
  await s.clock.advance(0);
  ok('命令绕过防抖立即派发', s.delivered.length === 1, '实际 ' + s.delivered.length);
  ok('命令内容未被合并',
    s.delivered.length === 1 && s.delivered[0].ev.message[0].data.text === '/new');
  s.restore();
}

console.log('\n══════ 多会话互不干扰 ══════');
{
  const s = makeScheduler();
  const KEY2 = 'private:999999';
  s.q.push(msg(1, 'A 说话'));
  s.q.noteTyping(KEY, true);           // 用户在打字
  s.q.noteTyping(KEY2, false);         // 另一人没打字
  await s.clock.advance(INBOUND_PLAIN_QUIET_MS + 500);
  ok('用户在打字 → 他的消息未派发', s.delivered.length === 0, '实际 ' + s.delivered.length);
  s.q.noteTyping(KEY, false);
  await s.clock.advance(INBOUND_TYPING_QUIET_MS + 100);
  ok('用户停止后派发', s.delivered.length === 1);
  s.restore();
}

console.log('\n══════════════════════════════════════════════');
console.log('通过 ' + pass + ' / 失败 ' + fail);
if (fail > 0) {
  console.log('失败项:');
  for (const f of failures) console.log('  ✗ ' + f);
  process.exit(1);
}
console.log('全部通过');

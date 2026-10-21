// 与页面共用同一投影实现，验证事件关联和增量语义；不请求外部模型。
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const assert = require("node:assert/strict");
const html = fs.readFileSync(
  require("node:path").join(
    __dirname,
    "../src/middlewares/trajectory/dashboard.html",
  ),
  "utf8",
);
const sandbox = { module: { exports: {} }, console };
vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], sandbox);
const { TrajectoryProjection, deriveLayout, virtualWindow } =
  sandbox.module.exports;
let ordinal = 0;
function event(kind, phase, span, data, extra = {}) {
  ordinal++;
  return {
    id: "event-" + ordinal,
    kind,
    phase,
    name: kind === "tool" ? "lookup" : kind,
    span_id: span,
    timestamp: new Date(1700000000000 + ordinal * 100).toISOString(),
    thread_id: "thread",
    run_id: "run",
    agent: "pm",
    data,
    ...extra,
  };
}

test("最终请求替换初始快照，调用时Schema不随后续请求改变，用量不重复累加", () => {
  const p = new TrajectoryProjection();
  const initial = event("context", "message", "m1", {
    round: 1,
    system: { content: "初始" },
    tools: [],
  });
  p.apply([initial, event("model", "start", "m1", {})]);
  p.apply([
    event("context", "message", "m1", {
      round: 1,
      system: { content: "注入后" },
      messages: [{ content: "输入" }],
      tools: [{ function: { name: "lookup", parameters: { version: 1 } } }],
    }),
    event("model", "end", "m1", {
      messages: [
        {
          content: "回复",
          usage_metadata: {
            input_tokens: 12,
            output_tokens: 4,
            input_token_details: { cache_read: 5 },
            output_token_details: { reasoning: 2 },
          },
        },
      ],
    }),
    event("tool", "start", "t1", { args: { customer: "甲" } }),
    event("tool", "end", "t1", { result: "完成" }),
  ]);
  const response = [...p.events.values()].find(
    (e) => e.kind === "model" && e.phase === "end",
  );
  p.apply([initial, response]);
  p.apply([
    event("context", "message", "m2", {
      round: 2,
      tools: [{ function: { name: "lookup", parameters: { version: 2 } } }],
    }),
  ]);
  const s = p.snapshot(),
    tool = s.records.find((r) => r.kind === "tool");
  assert.equal(s.records.filter((r) => r.kind === "message").length, 1);
  assert.equal(s.records.find((r) => r.kind === "system").text, "注入后");
  assert.equal(
    s.records.find((r) => r.kind === "message").input.system.content,
    "注入后",
  );
  assert.equal(tool.schema.function.parameters.version, 1);
  assert.equal(s.usage.input, 12);
  assert.equal(s.usage.think, 2);
  assert.equal(s.usage.cacheWrite, null);
});

test("原生更新合并到同一工具，消息分片拼接，上下文占用不混入Token账单", () => {
  const p = new TrajectoryProjection(),
    native = (span, update) =>
      event("native", "message", span, update, {
        agent: "claude",
        thread_id: "acp",
        run_id: "",
      });
  p.apply([
    native("a", {
      sessionUpdate: "agent_message_chunk",
      content: { type: "text", text: "正在" },
    }),
    native("b", {
      sessionUpdate: "agent_message_chunk",
      content: { type: "text", text: "检查" },
    }),
    native("c", {
      sessionUpdate: "tool_call",
      toolCallId: "call",
      title: "读取",
      rawInput: { path: "a" },
      status: "pending",
    }),
  ]);
  p.apply([
    native("d", {
      sessionUpdate: "tool_call_update",
      toolCallId: "call",
      rawOutput: "错误",
      status: "failed",
    }),
    native("u", { sessionUpdate: "usage_update", used: 9000, size: 1000000 }),
  ]);
  const s = p.snapshot();
  assert.equal(s.records.length, 2);
  assert.equal(s.records[0].text, "正在检查");
  assert.equal(s.records[1].input.path, "a");
  assert.equal(s.records[1].error, true);
  assert.equal(s.records[1].events.length, 2);
  assert.equal(s.usage.input, null);
  assert.equal(s.nativeUsage.size, 1);
});

test("旧日志可按相邻请求关联，没有Schema时保持缺失；取消不是失败", () => {
  const p = new TrajectoryProjection();
  p.apply([
    event("context", "message", "legacy-random", {
      round: 1,
      available_tools: ["lookup"],
    }),
    event("model", "start", "legacy-model", {}),
    event("model", "cancelled", "legacy-model", {}),
    event("tool", "start", "tool", {}),
    event("tool", "cancelled", "tool", {}),
  ]);
  const s = p.snapshot();
  assert.equal(s.records.find((r) => r.kind === "tool").schema, null);
  assert.equal(s.records.filter((r) => r.error).length, 0);
  assert.ok(s.records.find((r) => r.kind === "message").request);
});

test("过滤保留轮次分组，长历史只渲染可见窗口，折叠不删除记录", () => {
  const p = new TrajectoryProjection();
  p.apply(
    Array.from({ length: 10000 }, (_, i) =>
      event("input", "message", "i" + i, { content: "第" + i + "条" }),
    ),
  );
  const s = p.snapshot(),
    all = deriveLayout(s, () => true, new Set()),
    win = virtualWindow(all.rows, 170000, 340);
  assert.ok(win.rows.length <= 30);
  assert.ok(win.before > 0);
  assert.equal(
    win.before + win.rows.length * 30 + win.after,
    all.rows.length * 30,
  );
  const filtered = deriveLayout(s, (r) => r.text === "第9999条", new Set());
  assert.equal(filtered.matched.length, 1);
  assert.equal(filtered.rows[0].first, true);
  const closed = deriveLayout(s, () => true, new Set([s.records[0].turn]));
  assert.equal(closed.rows.length, 1);
  assert.equal(s.records.length, 10000);
});

test("Worker结束省略会话字段时仍合并到原委派记录", () => {
  const p = new TrajectoryProjection();
  p.apply([
    event(
      "worker",
      "start",
      "worker-turn",
      { payload: { text: "任务" } },
      { agent: "claude", thread_id: "native-session" },
    ),
    event(
      "worker",
      "error",
      "worker-turn",
      { error: "Connection closed" },
      { agent: "claude", thread_id: "" },
    ),
  ]);
  const records = p.snapshot().records;
  assert.equal(records.length, 1);
  assert.equal(records[0].error, true);
  assert.equal(records[0].events.length, 2);
});

test("交错返回的模型响应保持自己的请求绑定", () => {
  const p = new TrajectoryProjection();
  p.apply([
    event("model", "start", "parallel-a", {}),
    event("model", "start", "parallel-b", {}),
  ]);
  const before = p.snapshot().records.map((r) => r.request);
  p.apply([
    event("model", "end", "parallel-a", { messages: [{ content: "甲" }] }),
    event("model", "end", "parallel-b", { messages: [{ content: "乙" }] }),
  ]);
  const after = p.snapshot().records.map((r) => r.request);
  assert.equal(after[0], before[0]);
  assert.equal(after[1], before[1]);
  assert.notEqual(after[0], after[1]);
});

test("DSH布局不重复系统头，调用折叠按原请求绑定且历史有加载入口", () => {
  const p = new TrajectoryProjection();
  p.apply([
    event("context", "message", "one", {
      system: { content: "相同提示词" },
      tools: [],
    }),
    event("model", "start", "one", {}),
    event("model", "end", "one", { messages: [{ content: "第一步" }] }),
    event("tool", "start", "tool-one", { args: {} }),
    event("tool", "end", "tool-one", { result: "完成" }),
    event("context", "message", "two", {
      system: { content: "相同提示词" },
      tools: [],
    }),
    event("model", "start", "two", {}),
    event("model", "end", "two", { messages: [{ content: "第二步" }] }),
    event("tool", "start", "tool-two", { args: {} }),
    event("tool", "end", "tool-two", { result: "完成" }),
  ]);
  const snapshot = p.snapshot(),
    all = deriveLayout(snapshot, () => true);
  assert.equal(all.rows.length, 5);
  const firstRequest = snapshot.records.find(
    (r) => r.kind === "message",
  ).request;
  const folded = deriveLayout(
    snapshot,
    () => true,
    new Set(),
    new Set([firstRequest]),
  );
  assert.equal(folded.rows.filter((r) => r.record.kind === "tool").length, 1);
  const history = deriveLayout(snapshot, () => true, new Set(), new Set(), 2);
  assert.equal(history.rows[0].type, "history");
  assert.equal(history.rows[0].remaining, 3);
});

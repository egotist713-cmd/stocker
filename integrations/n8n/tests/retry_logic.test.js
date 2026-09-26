// Проверка логики Code-узлов stocker-retry и stocker-notify на подготовленных данных.
// Код берётся прямо из JSON workflow.
// Запуск (Node.js 18+, например в контейнере n8n):
//   node retry_logic.test.js /path/to/stocker-retry.json /path/to/stocker-notify.json
const assert = require("assert");
const fs = require("fs");

const retry = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const notify = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const code = (workflow, name) => workflow.nodes.find(n => n.name === name).parameters.jsCode;

// Минимальная эмуляция окружения Code-узла n8n.
function run(workflow, name, { input = [], nodes = {} }) {
  const $input = { all: () => input.map(json => ({ json })), first: () => ({ json: input[0] }) };
  const $ = node => ({
    all: () => (nodes[node] || []).map(json => ({ json })),
    first: () => ({ json: (nodes[node] || [])[0] }),
  });
  const fn = new Function("$input", "$", "$getWorkflowStaticData", code(workflow, name));
  const noStaticData = () => { throw new Error("static data must not be used: state lives in Stocker events"); };
  return fn($input, $, noStaticData).map(item => item.json);
}

const ok = data => ({ ok: true, data, error: null });
const failed = (stage, error) => ({ stage, status: "FAILED", message: { error_type: "ConnectionError", error } });
const sent = (kind, key) => ({ stage: "NOTIFY", status: "SENT", message: { kind, key, channel: "test" } });

// --- Candidates: Vision failed + только partial-черновики ----------------------------------
const candidates = run(retry, "Candidates", {
  input: [ok({ items: [
    { id: 13, filename: "d.jpg", pipeline: { metadata_completeness: "partial" } },
    { id: 14, filename: "e.jpg", pipeline: { metadata_completeness: "full" } },
  ] })],
  nodes: { "Vision failed": [ok({ items: [{ id: 10, filename: "a.jpg" }, { id: 11, filename: "b.jpg" }, { id: 12, filename: "c.jpg" }] })] },
});
assert.deepStrictEqual(candidates.map(c => [c.asset_id, c.operation]), [
  [10, "asset.process"], [11, "asset.process"], [12, "asset.process"], [13, "metadata.build"],
]);

// --- Plan: лимит по событиям стадии; «уже сообщено» — по NOTIFY/SENT в Stocker ---------------
const plan = run(retry, "Plan", {
  input: [
    ok([failed("AI", "down"), failed("METADATA_AI", "other stage")]),                 // 10: 1 неудача AI
    ok([failed("AI", "a"), failed("AI", "b"), failed("AI", "c")]),                       // 11: исчерпан, не сообщено
    ok([failed("AI", "a"), failed("AI", "b"), failed("AI", "c"), sent("retry_exhausted", "AI:3")]), // 12: уже сообщено
    { ok: false, data: null, error: { code: "INTERNAL" } },                              // 13: ошибка истории
  ],
  nodes: { Candidates: candidates },
});
assert.deepStrictEqual(plan.map(p => [p.asset_id, p.action, p.failed]), [
  [10, "retry", 1], [11, "exhausted", 3], [12, "exhausted_notified", 3], [13, "history_error", 0],
]);
assert.strictEqual(plan[1].exhausted_key, "AI:3");
assert.strictEqual(plan[1].last_error, "ConnectionError: c");

// --- Retry items: не более 5 за запуск -------------------------------------------------------
const many = Array.from({ length: 8 }, (_, i) => ({ asset_id: 100 + i, action: "retry", failed: 0 }));
const limited = run(retry, "Retry items", { input: [...many, { asset_id: 1, action: "exhausted" }] });
assert.strictEqual(limited.length, 5);

// --- Retry report: успех по ok; ключи уведомления «операция:попытка» -------------------------
const planned = [
  { asset_id: 10, filename: "a.jpg", operation: "asset.process", failed: 1 },
  { asset_id: 13, filename: "d.jpg", operation: "metadata.build", failed: 2 },
];
const [report] = run(retry, "Retry report", {
  input: [{ ok: true, outcome: "AI_PASSED" }, { ok: false, outcome: "METADATA_AI_FAILED", error: null }],
  nodes: { "Retry items": planned },
});
assert.strictEqual(report.severity, "warning");
assert.strictEqual(report.kind, "retry_report");
assert.deepStrictEqual(report.items, [{ asset_id: 10, key: "asset.process:2" }, { asset_id: 13, key: "metadata.build:3" }]);

// --- New exhausted: только не сообщённые в Stocker; без статических данных n8n ----------------
const [alert] = run(retry, "New exhausted", { input: plan });
assert.strictEqual(alert.kind, "retry_exhausted");
assert.deepStrictEqual(alert.items.map(i => i.asset_id), [11, 13]);           // 12 уже сообщён
assert.strictEqual(alert.items[0].key, "AI:3");
assert.ok(alert.items[1].key.startsWith("history_error:"));
assert.deepStrictEqual(run(retry, "New exhausted", { input: plan.filter(p => p.action === "retry" || p.action === "exhausted_notified") }), []);

// --- stocker-notify: в Stocker записываются только уведомления с объектами --------------------
const delivered = run(notify, "Test channel", { input: [alert, { severity: "info", title: "digest", text: "ok" }] });
assert.strictEqual(delivered[0].channel, "test");
const toRecord = run(notify, "To record", { input: delivered });
assert.strictEqual(toRecord.length, 1);
assert.deepStrictEqual(Object.keys(toRecord[0]).sort(), ["channel", "items", "kind", "severity", "title"]);

console.log("retry/notify logic: all checks passed");

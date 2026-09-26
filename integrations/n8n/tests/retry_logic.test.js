// Проверка логики Code-узлов stocker-retry на подготовленных данных.
// Код берётся прямо из integrations/n8n/workflows/stocker-retry.json.
// Запуск (Node.js 18+, например в контейнере n8n):
//   node retry_logic.test.js /path/to/stocker-retry.json
const assert = require("assert");
const fs = require("fs");

const workflow = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const code = name => workflow.nodes.find(n => n.name === name).parameters.jsCode;

// Минимальная эмуляция окружения Code-узла n8n.
function run(name, { input = [], nodes = {}, staticData = {} }) {
  const $input = { all: () => input.map(json => ({ json })), first: () => ({ json: input[0] }) };
  const $ = node => ({
    all: () => (nodes[node] || []).map(json => ({ json })),
    first: () => ({ json: (nodes[node] || [])[0] }),
  });
  const $getWorkflowStaticData = () => staticData;
  const fn = new Function("$input", "$", "$getWorkflowStaticData", code(name));
  return fn($input, $, $getWorkflowStaticData).map(item => item.json);
}

const ok = data => ({ ok: true, data, error: null });
const failedEvent = (stage, error) => ({ stage, status: "FAILED", message: { error_type: "ConnectionError", error } });

// --- Candidates: Vision failed + только partial-черновики ----------------------------------
const candidates = run("Candidates", {
  input: [ok({ items: [
    { id: 13, filename: "d.jpg", pipeline: { metadata_completeness: "partial" } },
    { id: 14, filename: "e.jpg", pipeline: { metadata_completeness: "full" } },
  ] })],
  nodes: { "Vision failed": [ok({ items: [{ id: 10, filename: "a.jpg" }, { id: 11, filename: "b.jpg" }] })] },
});
assert.deepStrictEqual(candidates.map(c => [c.asset_id, c.operation]), [
  [10, "asset.process"], [11, "asset.process"], [13, "metadata.build"],
]);

// --- Plan: лимит 3 неудачи по событиям Stocker; ошибка истории -----------------------------
const plan = run("Plan", {
  input: [
    ok([failedEvent("AI", "down")]),
    ok([failedEvent("AI", "a"), failedEvent("AI", "b"), failedEvent("AI", "c")]),
    { ok: false, data: null, error: { code: "INTERNAL" } },
  ],
  nodes: { Candidates: candidates },
});
assert.deepStrictEqual(plan.map(p => [p.asset_id, p.action, p.failed]), [
  [10, "retry", 1], [11, "exhausted", 3], [13, "history_error", 0],
]);
assert.strictEqual(plan[1].last_error, "ConnectionError: c");

// --- Retry items: не более 5 за запуск -------------------------------------------------------
const many = Array.from({ length: 8 }, (_, i) => ({ asset_id: 100 + i, action: "retry", failed: 0 }));
const limited = run("Retry items", { input: [...many, { asset_id: 1, action: "exhausted" }] });
assert.strictEqual(limited.length, 5);
assert.ok(limited.every(i => i.action === "retry"));

// --- Retry report: успех определяет ok из ответа Stocker ------------------------------------
const planned = [
  { asset_id: 10, filename: "a.jpg", operation: "asset.process", failed: 1 },
  { asset_id: 13, filename: "d.jpg", operation: "metadata.build", failed: 2 },
];
const [report] = run("Retry report", {
  input: [{ ok: true, outcome: "AI_PASSED" }, { ok: false, outcome: "METADATA_AI_FAILED", error: null }],
  nodes: { "Retry items": planned },
});
assert.strictEqual(report.severity, "warning");
assert.deepStrictEqual(report.data.results.map(r => [r.asset_id, r.recovered, r.attempt]), [[10, true, 2], [13, false, 3]]);

// --- New exhausted: уведомление только о новых ----------------------------------------------
const store = {};
const first = run("New exhausted", { input: plan, staticData: store });
assert.strictEqual(first.length, 1);
assert.strictEqual(first[0].severity, "warning");
assert.ok(first[0].text.includes("#11") && first[0].text.includes("#13"));
assert.deepStrictEqual(run("New exhausted", { input: plan, staticData: store }), []); // повтор — тишина

const moreFailures = plan.map(p => (p.asset_id === 11 ? { ...p, failed: 4 } : p));
assert.strictEqual(run("New exhausted", { input: moreFailures, staticData: store }).length, 1); // новое число неудач

console.log("retry logic: all checks passed");

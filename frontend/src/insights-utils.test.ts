import test from "node:test";
import assert from "node:assert/strict";
import { advanceReceiptHistory, buildTrend, formatClock, formatMoney, formatPercent, validRecommendations } from "./insights-utils";
import type { AgentRun, AvailableAction, PosEvent, Recommendation, TimePoint } from "./types";

const event = (id: string): PosEvent => ({
  id, at: "2026-10-01T20:00:00Z", elapsedSeconds: 1, sku: "sku", item: "Jeans",
  zoneId: "denim", size: "M", units: 1, amountCents: 19990, type: "sale", provenance: "simulated-pos",
});
const point = (elapsedSeconds: number, revenueCents: number, controlRevenueCents: number): TimePoint => ({
  at: "2026-10-01T20:00:00Z", elapsedSeconds, revenueCents, controlRevenueCents, visits: 1, transactions: 1,
});
const recommendation: Recommendation = {
  id: "recommendation-1", actionId: "reposition-denim", title: "Reorganizar denim",
  rationale: "Receita simulada abaixo do controle.", evidenceIds: ["pos-1"], caveats: [],
  priority: "medium", requiresApproval: true,
};
const action: AvailableAction = {
  id: "reposition-denim", title: "Reposicionar denim", description: "Ajustar o layout simulado",
  zoneId: "denim", guardrails: ["Somente simulação"], applied: false,
};
const run: AgentRun = {
  id: "run-1", agent: "merchandising", status: "completed", createdAt: "2026-10-01T20:00:00Z",
  completedAt: "2026-10-01T20:00:02Z", agentName: "vitrine-merchandising", agentVersion: "1",
  responseId: "response-1", conversationId: null, toolCalls: ["get_snapshot"],
  summary: "Comparação simulada.", recommendations: [recommendation], limitations: [],
  error: null, execution: "azure-foundry-agent",
};

test("Portuguese formatting preserves null, actual zero, negative amounts and percentages", () => {
  assert.equal(formatMoney(null), "Dados insuficientes");
  assert.equal(formatPercent(null), "Dados insuficientes");
  assert.equal(formatMoney(Number.NaN), "Dados insuficientes");
  assert.match(formatMoney(0), /0,00/);
  assert.match(formatMoney(-12345, true), /123,45/);
  assert.match(formatMoney(123.45), /123,45/);
  assert.equal(formatPercent(0), "0%");
  assert.equal(formatPercent(12.5), "+12,5%");
  assert.equal(formatPercent(-2), "-2%");
  assert.equal(formatClock(3661), "1:01:01");
  assert.equal(formatClock(65.9), "01:05");
  assert.equal(formatClock(-1), "—");
});

test("trend has no fabricated baseline, uses real controls and never mutates the source", () => {
  const input = [point(30, 5000, 4500), point(10, 1000, 1200)];
  const original = [...input];
  const trend = buildTrend(input)!;
  assert.deepEqual(input, original);
  assert.equal(trend.points.length, 2);
  assert.equal(trend.minTime, 10);
  assert.equal(trend.maxTime, 30);
  assert.equal(trend.points[0], input[1]);
  assert.equal(trend.controlPath.split(" ").length, 2);
  assert.equal(trend.revenuePath.split(" ").length, 2);
  assert.equal(buildTrend([]), null);
});

test("trend supports single point, all-zero, negative revenue and invalid samples", () => {
  for (const sample of [point(0, 0, 0), point(10, -1000, -500)]) {
    const trend = buildTrend([sample])!;
    assert.equal(trend.x(sample.elapsedSeconds), 386);
    assert.ok(Number.isFinite(trend.y(sample.revenueCents)));
    assert.ok(!trend.revenuePath.includes("NaN"));
  }
  assert.equal(buildTrend([point(1, Number.NaN, 20)]), null);
  assert.equal(buildTrend([point(-1, 100, 20)]), null);
  assert.deepEqual(buildTrend([point(0, 0, 0)])!.ticks, [0]);
});

test("initial receipt hydration never animates; only unseen backend IDs animate", () => {
  const initial = advanceReceiptHistory(null, [event("a")], 10, "scenario-1");
  assert.equal(initial.fresh.size, 0);
  const next = advanceReceiptHistory(initial, [event("a"), event("b")], 11, "scenario-1");
  assert.deepEqual([...next.fresh], ["b"]);
  assert.deepEqual([...initial.seen], ["a"]);
  const expired = { ...next, fresh: new Set<string>() };
  const repeated = advanceReceiptHistory(expired, [event("a"), event("b")], 12, "scenario-1");
  assert.equal(repeated.fresh.size, 0);
});

test("reset by scenario or rewound clock does not animate reused or new IDs", () => {
  const initial = advanceReceiptHistory(null, [event("a")], 10, "scenario-1");
  const next = advanceReceiptHistory(initial, [event("b")], 11, "scenario-1");
  assert.equal(next.fresh.size, 1);
  const scenarioReset = advanceReceiptHistory(next, [event("b"), event("c")], 12, "scenario-2");
  assert.equal(scenarioReset.fresh.size, 0);
  assert.deepEqual([...scenarioReset.seen], ["b", "c"]);
  const clockReset = advanceReceiptHistory(next, [event("c")], 0, "scenario-1");
  assert.equal(clockReset.fresh.size, 0);
  assert.deepEqual([...clockReset.seen], ["c"]);
});

test("rolling feed retains seen IDs and removes obsolete animations", () => {
  const initial = advanceReceiptHistory(null, [event("a")], 1, "s");
  const next = advanceReceiptHistory(initial, [event("b")], 2, "s");
  const restored = advanceReceiptHistory(next, [event("a")], 3, "s");
  assert.equal(restored.fresh.size, 0);
  assert.deepEqual([...restored.seen], ["a", "b"]);
});

test("reduced motion removes active animations and suppresses new ones", () => {
  const initial = advanceReceiptHistory(null, [event("a")], 1, "s");
  const next = advanceReceiptHistory(initial, [event("b")], 2, "s");
  const reduced = advanceReceiptHistory(next, [event("b"), event("c")], 3, "s", true);
  assert.equal(reduced.fresh.size, 0);
  const restored = advanceReceiptHistory(reduced, [event("b"), event("c")], 4, "s");
  assert.equal(restored.fresh.size, 0);
});

test("only completed successful runs with matching available actions yield cards", () => {
  const valid = validRecommendations(run, [action]);
  assert.equal(valid.length, 1);
  assert.equal(valid[0].recommendation, recommendation);
  assert.equal(valid[0].action, action);
  for (const status of ["queued", "running", "failed"] as const) {
    assert.deepEqual(validRecommendations({ ...run, status }, [action]), []);
  }
  assert.deepEqual(validRecommendations({ ...run, error: "failed" }, [action]), []);
  assert.deepEqual(validRecommendations(run, []), []);
  assert.equal(validRecommendations(run, [{ ...action, applied: true }])[0].action.applied, true);
});

test("malformed, unsupported, approval-free, evidence-free and duplicate recommendations are rejected", () => {
  const invalid = [
    { ...recommendation, title: "" }, { ...recommendation, rationale: " " },
    { ...recommendation, priority: "urgent" }, { ...recommendation, requiresApproval: false },
    { ...recommendation, evidenceIds: [] }, { ...recommendation, evidenceIds: [1] },
    { ...recommendation, caveats: null }, { ...recommendation, actionId: "unknown" },
  ];
  for (const value of invalid) {
    assert.deepEqual(validRecommendations({ ...run, recommendations: [value as Recommendation] }, [action]), []);
  }
  assert.equal(validRecommendations({ ...run, recommendations: [recommendation, recommendation] }, [action]).length, 1);
});

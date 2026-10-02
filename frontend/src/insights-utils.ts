import type { AgentRun, AvailableAction, PosEvent, Recommendation, TimePoint } from "./types";

const currency = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
const number = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1 });

export function formatMoney(value: number | null, inCents = false): string {
  return value === null || !Number.isFinite(value)
    ? "Dados insuficientes"
    : currency.format(inCents ? value / 100 : value);
}

export function formatPercent(value: number | null): string {
  return value === null || !Number.isFinite(value)
    ? "Dados insuficientes"
    : `${value > 0 ? "+" : ""}${number.format(value)}%`;
}

export function formatClock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = String(total % 60).padStart(2, "0");
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${remainder}`
    : `${String(minutes).padStart(2, "0")}:${remainder}`;
}

export function buildTrend(series: TimePoint[]) {
  const points = series
    .filter((point) => Number.isFinite(point.elapsedSeconds) && point.elapsedSeconds >= 0
      && Number.isFinite(point.revenueCents) && Number.isFinite(point.controlRevenueCents))
    .slice()
    .sort((a, b) => a.elapsedSeconds - b.elapsedSeconds);
  if (!points.length) return null;
  const minTime = points[0].elapsedSeconds;
  const maxTime = points[points.length - 1].elapsedSeconds;
  let minimum = 0;
  let maximum = 0;
  for (const point of points) {
    minimum = Math.min(minimum, point.revenueCents, point.controlRevenueCents);
    maximum = Math.max(maximum, point.revenueCents, point.controlRevenueCents);
  }
  const valueSpan = maximum - minimum || 100;
  const x = (time: number) => maxTime === minTime
    ? 386
    : 82 + ((time - minTime) / (maxTime - minTime)) * 610;
  const y = (value: number) => 190 - ((value - minimum) / valueSpan) * 158;
  return {
    points,
    minTime,
    maxTime,
    x,
    y,
    ticks: maximum === minimum ? [minimum] : [minimum, minimum + valueSpan / 2, maximum],
    revenuePath: points.map((point) => `${x(point.elapsedSeconds)},${y(point.revenueCents)}`).join(" "),
    controlPath: points.map((point) => `${x(point.elapsedSeconds)},${y(point.controlRevenueCents)}`).join(" "),
  };
}

export interface ReceiptHistory {
  scenarioId: string;
  elapsedSeconds: number;
  seen: ReadonlySet<string>;
  fresh: ReadonlySet<string>;
}

export function advanceReceiptHistory(
  previous: ReceiptHistory | null,
  events: PosEvent[],
  elapsedSeconds: number,
  scenarioId: string,
  reducedMotion = false,
): ReceiptHistory {
  const reset = !previous || previous.scenarioId !== scenarioId
    || elapsedSeconds < previous.elapsedSeconds;
  const seen = new Set(reset ? [] : previous.seen);
  const visible = new Set(events.map((event) => event.id));
  const fresh = new Set(reset || reducedMotion ? [] : [...previous.fresh].filter((id) => visible.has(id)));
  for (const event of events) {
    if (!reset && !reducedMotion && !seen.has(event.id)) fresh.add(event.id);
    seen.add(event.id);
  }
  return { scenarioId, elapsedSeconds, seen, fresh };
}

const nonempty = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;
const stringList = (value: unknown): value is string[] => Array.isArray(value) && value.every(nonempty);

export function validRecommendations(
  run: AgentRun,
  actions: AvailableAction[],
): Array<{ recommendation: Recommendation; action: AvailableAction }> {
  if (run.status !== "completed" || run.error || run.execution !== "azure-foundry-agent"
    || !Array.isArray(run.recommendations)) return [];
  const ids = new Set<string>();
  return run.recommendations.flatMap((recommendation) => {
    if (!recommendation || !nonempty(recommendation.id) || ids.has(recommendation.id)
      || !nonempty(recommendation.title) || !nonempty(recommendation.rationale)
      || recommendation.requiresApproval !== true
      || !["high", "medium", "low"].includes(recommendation.priority)
      || !stringList(recommendation.evidenceIds) || !recommendation.evidenceIds.length
      || !stringList(recommendation.caveats)) return [];
    const action = actions.find((candidate) => candidate.id === recommendation.actionId);
    if (!action) return [];
    ids.add(recommendation.id);
    return [{ recommendation, action }];
  });
}

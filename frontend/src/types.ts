export type ZoneId = "denim" | "essentials" | "checkout";
export type AgentKind = "merchandising" | "experiment";
export type ActionId = "reposition-denim" | "replenish-essentials";
export interface TimePoint {
  at: string;
  elapsedSeconds: number;
  revenueCents: number;
  transactions: number;
  controlRevenueCents: number;
  visits: number;
}
export interface Zone {
  id: ZoneId;
  name: string;
  collection: string;
  colorRole: "accent" | "link" | "success";
  visitors: number;
  dwellSeconds: number;
  revenueCents: number;
  units: number;
  availableSizes: string[];
  stockStatus: "available" | "limited" | "replenished";
  observation: string;
  provenance: "simulation";
}
export interface Detection {
  label: "person";
  score: number;
  box: [number, number, number, number];
  // Normalized xyxy coordinates, measured on frameUri at sourceSeconds, not the moving stream.
}
export interface VisionObservation {
  id: string;
  cameraId: string;
  status: "ready" | "warming" | "unavailable";
  observedAt: string | null;
  sourceSeconds: number | null;
  people: number | null;
  detections: Detection[];
  frameUri: string | null;
  latencyMs: number | null;
  model: string;
  modelRevision: string;
  provenance: "model-inference";
  error: string | null;
}
export interface Camera {
  id: string;
  name: string;
  zoneId: ZoneId;
  mediaUri: string;
  variant: "baseline" | "optimized";
  durationSeconds: number;
  sourceSeconds: number;
  footage: "synthetic-3d";
  vision: VisionObservation;
}
export interface PosEvent {
  id: string;
  at: string;
  elapsedSeconds: number;
  sku: string;
  item: string;
  zoneId: ZoneId;
  size: string;
  units: number;
  amountCents: number;
  type: "sale" | "return";
  provenance: "simulated-pos";
}
export interface AvailableAction {
  id: ActionId;
  title: string;
  description: string;
  zoneId: ZoneId;
  guardrails: string[];
  applied: boolean;
}
export interface Recommendation {
  id: string;
  actionId: ActionId;
  title: string;
  rationale: string;
  evidenceIds: string[];
  caveats: string[];
  priority: "high" | "medium" | "low";
  requiresApproval: true;
}
export interface AgentRun {
  id: string;
  agent: AgentKind;
  status: "queued" | "running" | "completed" | "failed";
  createdAt: string;
  completedAt: string | null;
  agentName: string;
  agentVersion: string | null;
  responseId: string | null;
  conversationId: string | null;
  toolCalls: string[];
  summary: string | null;
  recommendations: Recommendation[];
  limitations: string[];
  error: string | null;
  execution: "azure-foundry-agent";
}
export interface Intervention {
  id: string;
  actionId: ActionId;
  title: string;
  at: string;
  elapsedSeconds: number;
  layoutVersion: number;
  sourceRunId: string;
  provenance: "approved-simulation";
}
export interface Experiment {
  status: "not-started" | "collecting" | "simulated-comparison";
  interventionAt: string | null;
  beforeRevenuePer100Visits: number | null;
  afterRevenuePer100Visits: number | null;
  controlChangePercent: number | null;
  relativeChangePercent: number | null;
  beforeTransactions: number;
  afterTransactions: number;
  caveat: string;
}
export interface Snapshot {
  serverTime: string;
  revision: number;
  store: { id: string; name: string; areaM2: number; description: string };
  scenario: {
    id: string;
    state: "running" | "paused";
    elapsedSeconds: number;
    speed: number;
    layoutVersion: number;
    simulation: true;
    activeSince: string;
    label: string;
  };
  metrics: {
    revenueCents: number;
    transactions: number;
    units: number;
    averageBasketCents: number;
    simulatedVisits: number;
    provenance: "simulated-pos";
  };
  zones: Zone[];
  cameras: Camera[];
  posEvents: PosEvent[];
  series: TimePoint[];
  availableActions: AvailableAction[];
  interventions: Intervention[];
  agentRuns: AgentRun[];
  experiment: Experiment;
  services: {
    foundry: "configured" | "unavailable";
    vision: "ready" | "warming" | "unavailable";
    persistence: "cosmos" | "local";
    footageReady: boolean;
  };
  notices: string[];
}
export interface PublicConfig {
  name: string;
  authRequired: boolean;
  mode: "azure" | "local";
  simulation: true;
  model: string;
  agents: Array<{ kind: AgentKind; name: string }>;
}

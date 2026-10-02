import type { ActionId, AgentKind, AgentRun, Intervention, PublicConfig, Snapshot } from "./types";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

const messages: Record<number, string> = {
  401: "Sua sessão expirou. Entre novamente para continuar.",
  403: "A origem desta solicitação não foi autorizada.",
  409: "O cenário mudou ou esta ação já não é válida. Revise os dados atuais.",
  429: "Muitas solicitações. Aguarde um momento antes de tentar novamente.",
  503: "O serviço está indisponível no momento. Nenhum resultado foi inventado.",
};

export async function request<T>(path: string, options: {
  method?: "GET" | "POST" | "DELETE"; body?: unknown; signal?: AbortSignal; timeoutMs?: number;
} = {}): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort(options.signal?.reason);
  options.signal?.addEventListener("abort", abort, { once: true });
  if (options.signal?.aborted) abort();
  const timeout = setTimeout(() => controller.abort(new Error("Tempo limite excedido.")), options.timeoutMs ?? 25_000);
  try {
    const response = await fetch(path, {
      method: options.method || "GET",
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      headers: options.body === undefined ? { Accept: "application/json" } : {
        Accept: "application/json", "Content-Type": "application/json",
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    });
    if (response.status === 204) return undefined as T;
    const text = await response.text();
    let data: unknown;
    try { data = text ? JSON.parse(text) : undefined; } catch {
      throw new ApiError(response.status, "invalid_response", "A API retornou uma resposta inválida.");
    }
    if (!response.ok) {
      const error = data as { error?: string; detail?: string } | undefined;
      throw new ApiError(response.status, error?.error || "request_failed",
        [messages[response.status] || "Não foi possível concluir a solicitação.", error?.detail].filter(Boolean).join(" "));
    }
    return data as T;
  } catch (error) {
    if (options.signal?.aborted) throw error;
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, "connection_failed", "Não foi possível conectar à API. Verifique a conexão e tente novamente.");
  } finally {
    clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abort);
  }
}

export const api = {
  config: (signal: AbortSignal) => request<PublicConfig>("/api/config", { signal }),
  session: (signal: AbortSignal) => request<{ authenticated: boolean }>("/api/session", { signal }),
  login: (password: string, signal: AbortSignal) =>
    request<{ authenticated: boolean }>("/api/session", { method: "POST", body: { password }, signal }),
  logout: (signal: AbortSignal) => request<void>("/api/session", { method: "DELETE", signal }),
  snapshot: (signal: AbortSignal) => request<Snapshot>("/api/snapshot", { signal, timeoutMs: 8_000 }),
  control: (action: "pause" | "resume" | "reset", speed: 1 | 2 | 5 | undefined, signal: AbortSignal) =>
    request<Snapshot>("/api/scenario/control", { method: "POST", body: { action, ...(speed ? { speed } : {}) }, signal }),
  run: (agent: AgentKind, question: string, signal: AbortSignal) =>
    request<AgentRun>("/api/agents/run", { method: "POST", body: { agent, question }, signal }),
  getRun: (id: string, signal: AbortSignal) => request<AgentRun>(`/api/agents/runs/${encodeURIComponent(id)}`, { signal, timeoutMs: 8_000 }),
  intervene: (runId: string, recommendationId: string, actionId: ActionId, signal: AbortSignal) =>
    request<Intervention>("/api/interventions", { method: "POST", body: { runId, recommendationId, actionId }, signal }),
};

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import type { AgentRun, PublicConfig, Snapshot } from "./types";

export function useRetail() {
  const [auth, setAuth] = useState<"loading" | "in" | "out" | "error">("loading");
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const [lastSync, setLastSync] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [boot, setBoot] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const epoch = useRef(0);
  const mutation = useRef<AbortController | null>(null);
  const poll = useRef<AbortController | null>(null);
  const runCache = useRef<AgentRun[]>([]);
  const scenario = useRef<string | null>(null);

  const invalidate = useCallback(() => {
    epoch.current += 1;
    poll.current?.abort();
    mutation.current?.abort();
    mutation.current = null;
    setBusy(false);
  }, []);

  const expired = useCallback(() => {
    invalidate();
    setAuth("out"); setSnapshot(null); setRuns([]); runCache.current = [];
    setConnected(false); setLastSync(null); scenario.current = null;
    setSyncError(null);
    setError("Sua sessão expirou. Entre novamente para continuar.");
  }, [invalidate]);

  const acceptSnapshot = useCallback((data: Snapshot) => {
    const identity = `${data.scenario.id}:${data.scenario.activeSince}`;
    if (scenario.current !== identity) {
      runCache.current = [];
      scenario.current = identity;
    }
    setSnapshot(data);
    const merged = new Map(data.agentRuns.map(run => [run.id, run]));
    for (const run of runCache.current) {
      const current = merged.get(run.id);
      if (!current || (current.status !== "completed" && current.status !== "failed")) merged.set(run.id, run);
    }
    runCache.current = [...merged.values()];
    setRuns(runCache.current);
    setConnected(true); setLastSync(Date.now());
    setSyncError(null);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setAuth("loading"); setError(null);
    void Promise.all([api.config(controller.signal), api.session(controller.signal)])
      .then(([configuration, session]) => {
        if (controller.signal.aborted) return;
        setConfig(configuration); setAuth(session.authenticated ? "in" : "out");
      }).catch(reason => {
        if (controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : "Não foi possível iniciar a aplicação.");
        setAuth("error");
      });
    return () => controller.abort();
  }, [boot]);

  useEffect(() => {
    if (auth !== "in") return;
    let timer: ReturnType<typeof setTimeout>;
    let stopped = false;
    const generation = epoch.current;
    const cycle = async () => {
      if (stopped || mutation.current || generation !== epoch.current) return;
      const controller = new AbortController();
      poll.current = controller;
      try {
        const data = await api.snapshot(controller.signal);
        if (stopped || controller.signal.aborted || generation !== epoch.current) return;
        acceptSnapshot(data);
        // Agent run status uses the dedicated endpoint, serialized after the snapshot.
        for (const run of runCache.current.filter(item => item.status === "queued" || item.status === "running")) {
          const updated = await api.getRun(run.id, controller.signal);
          if (stopped || controller.signal.aborted || generation !== epoch.current) return;
          runCache.current = runCache.current.map(item => item.id === updated.id ? updated : item);
          setRuns([...runCache.current]);
        }
      } catch (reason) {
        if (stopped || controller.signal.aborted) return;
        if (reason instanceof ApiError && reason.status === 401) { expired(); return; }
        setConnected(false);
        setSyncError(reason instanceof Error ? reason.message : "Conexão interrompida.");
      } finally {
        if (poll.current === controller) poll.current = null;
        if (!stopped && generation === epoch.current) timer = setTimeout(cycle, 2_000);
      }
    };
    void cycle();
    return () => { stopped = true; clearTimeout(timer); poll.current?.abort(); };
  }, [auth, refresh, acceptSnapshot, expired]);

  useEffect(() => () => { invalidate(); }, [invalidate]);

  const act = async <T,>(operation: (signal: AbortSignal) => Promise<T>, apply?: (value: T) => void): Promise<boolean> => {
    if (mutation.current) return false;
    epoch.current += 1;
    poll.current?.abort();
    const generation = epoch.current;
    const controller = new AbortController();
    mutation.current = controller;
    setBusy(true); setError(null);
    try {
      const result = await operation(controller.signal);
      if (controller.signal.aborted || generation !== epoch.current) return false;
      apply?.(result);
      return true;
    } catch (reason) {
      if (controller.signal.aborted) return false;
      if (reason instanceof ApiError && reason.status === 401) expired();
      else setError(reason instanceof Error ? reason.message : "Não foi possível concluir a ação.");
      return false;
    } finally {
      if (mutation.current === controller) {
        mutation.current = null;
        setBusy(false);
        setRefresh(value => value + 1);
      }
    }
  };

  return {
    auth, config, snapshot, connected, lastSync, error: error || syncError, busy, runs, act, acceptSnapshot,
    dismissError: () => { setError(null); setSyncError(null); },
    retry: () => { setError(null); setSyncError(null); if (auth === "error") setBoot(value => value + 1); else setRefresh(value => value + 1); },
    login: (password: string) => act(signal => api.login(password, signal), session => {
      if (!session.authenticated) throw new ApiError(401, "login_failed", "Não foi possível autenticar.");
      setAuth("in");
    }),
    logout: () => act(signal => api.logout(signal), () => {
      setAuth("out"); setSnapshot(null); setConnected(false); setLastSync(null);
      setSyncError(null);
      setRuns([]); runCache.current = []; scenario.current = null;
    }),
    rememberRun: (run: AgentRun) => {
      runCache.current = [run, ...runCache.current.filter(item => item.id !== run.id)];
      setRuns([...runCache.current]);
    },
  };
}

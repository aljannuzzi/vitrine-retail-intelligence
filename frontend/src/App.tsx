import { Component, useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api } from "./api";
import { useRetail } from "./useRetail";
import { CameraWall } from "./CameraWall";
import { FloorPlan } from "./FloorPlan";
import { AgentPanel, ExperimentPanel, ReceiptFeed, SalesTrend } from "./Insights";
import { formatClock, formatMoney, validRecommendations } from "./insights-utils";
import type { AgentKind, AgentRun, Recommendation, ZoneId } from "./types";

function Icon({ name }: { name: "arrow" | "pause" | "play" | "reset" | "sun" | "exit" | "lock" }) {
  const paths = {
    arrow: <><path d="M5 12h14M13 6l6 6-6 6" /></>,
    pause: <><path d="M8 5v14M16 5v14" /></>,
    play: <path d="m8 5 11 7-11 7Z" />,
    reset: <><path d="M4 10a8 8 0 1 1 1 7M4 4v6h6" /></>,
    sun: <><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1 1M18 18l1 1M5 19l1-1M18 6l1-1" /></>,
    exit: <><path d="M10 4H4v16h6M10 12h11M17 8l4 4-4 4" /></>,
    lock: <><rect x="5" y="10" width="14" height="11" rx="2" /><path d="M8 10V6a4 4 0 0 1 8 0v4M12 14v3" /></>,
  };
  return <svg className="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

function Wordmark() {
  return <a className="brand" href="#main" aria-label="Vitrine — ir para conteúdo"><span className="brand-symbol" aria-hidden="true">V</span><span>VITRINE<small>RETAIL INTELLIGENCE</small></span></a>;
}

function ThemeButton() {
  const [dark, setDark] = useState(() => document.documentElement.dataset.theme === "dark");
  return <button className="button button-icon" title={dark ? "Usar tema claro" : "Usar tema escuro"}
    aria-label={dark ? "Usar tema claro" : "Usar tema escuro"} aria-pressed={dark} onClick={() => {
      document.documentElement.dataset.theme = dark ? "light" : "dark"; setDark(!dark);
    }}><Icon name="sun" /></button>;
}

function WallClock() {
  const [time, setTime] = useState(() => new Date());
  useEffect(() => { const interval = setInterval(() => setTime(new Date()), 1_000); return () => clearInterval(interval); }, []);
  return <time className="wall-clock" dateTime={time.toISOString()} title="Hora local, independente do relógio da simulação">{time.toLocaleTimeString("pt-BR")}<span>HORA LOCAL</span></time>;
}

function Provenance() {
  const [open, setOpen] = useState(false);
  return <div className="provenance-bar">
    <span className="provenance-item"><i className="origin-mark origin-simulation" />Fonte & PDV <b>simulados</b></span>
    <span className="provenance-item"><i className="origin-mark origin-model" />Visão <b>inferência real</b></span>
    <span className="provenance-item"><i className="origin-mark origin-agent" />Agentes <b>Foundry real</b></span>
    <div className="provenance-help">
      <button className="button button-ghost" aria-expanded={open} aria-controls="provenance-explanation"
        title="Vídeos e vendas são sintéticos. Caixas vêm do modelo e recomendações de execuções reais do Foundry. Não há identificação de pessoas nem prova causal."
        onClick={() => setOpen(!open)}>Como ler estes dados <span aria-hidden="true">ⓘ</span></button>
      {open && <div id="provenance-explanation" className="provenance-popover">
        <strong>Três origens. Nenhuma ambiguidade.</strong>
        <p>Vídeos, visitas e vendas são sintéticos. Contagens por quadro e caixas vêm exclusivamente da inferência do modelo. Recomendações vêm de execuções reais de agentes registrados no Foundry.</p>
        <p>As câmeras não identificam pessoas, não medem vendas e não são somadas para calcular visitantes únicos. Experimentos não demonstram ganho causal real.</p>
        <button className="button button-ghost" onClick={() => setOpen(false)}>Entendi</button>
      </div>}
    </div>
  </div>;
}

function StoreIllustration() {
  return <svg className="store-illustration" viewBox="0 0 560 410" role="img" aria-label="Ilustração arquitetônica da loja conceito, não uma câmera ou medição">
    <path className="illustration-floor" d="m45 294 240-125 235 126-239 125Z" />
    <path className="illustration-wall" d="M45 294V112L285 0v169Zm240-125V0l235 125v170Z" />
    <g className="illustration-lines"><path d="m45 112 240 123 235-110M285 235v185M85 235v-77l104 53v78M375 225v-75l104 56v75" />
      <path d="m74 277 143 76M84 276v-81l123 65v82m-103-131 0 67m20-57v68m20-57v66m20-56v67m20-56v65" />
      <path d="m330 304 103-53M340 299v-76l83-43v77m-66-42v65m19-75v67m19-77v66" />
      <path d="m192 139 49-25 57 31-49 25Zm0 0v15l57 31 49-26v-14m-49 25v15" />
    </g>
    <g className="illustration-clothes"><path d="m93 205 10-8 12 16 6 39-28-15Zm39 21 10-9 12 17 7 40-29-15Zm40 21 10-8 12 17 7 40-29-16Z" />
      <path d="m355 220 11-17 11 7 2 33-25 13Zm34-18 11-17 11 7 2 33-25 13Z" /></g>
    <text x="323" y="99" transform="rotate(28 323 99)">VITRINE</text>
  </svg>;
}

function Gate({ retail }: { retail: ReturnType<typeof useRetail> }) {
  const passwordRef = useRef<HTMLInputElement>(null);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const password = passwordRef.current?.value || "";
    if (!password) return;
    const result = retail.login(password);
    if (passwordRef.current) passwordRef.current.value = "";
    void result;
  };
  return <div className="gate">
    <header className="gate-header"><Wordmark /><ThemeButton /></header>
    <main id="main" className="gate-main">
      <section className="gate-story"><span className="eyebrow">LOJA CONCEITO / EXPERIÊNCIA DEMONSTRATIVA</span>
        <h1>Um novo olhar.<br /><em>Novas possibilidades.</em></h1>
        <p>Entre em uma loja de moda inteiramente sintética. Acompanhe sua operação, explore observações reais de visão computacional e transforme recomendações de agentes em decisões humanas — com cada fonte de dados à vista.</p>
        <StoreIllustration /><span className="gate-caption">DESENHADA PARA EXPLORAR. NÃO PARA IDENTIFICAR.</span>
      </section>
      <section className="gate-card">
        <span className="gate-lock"><Icon name="lock" /></span><span className="eyebrow">SEU ACESSO À LOJA</span>
        <h2>A inteligência começa<br />com um olhar atento.</h2>
        {retail.auth === "loading" ? <div className="empty-state" role="status"><span className="spinner" />Conectando à experiência…</div>
          : retail.auth === "error" ? <><p className="error-message" role="alert">{retail.error}</p><button className="button button-primary" onClick={retail.retry}>Tentar conexão novamente</button></>
          : <form onSubmit={submit}>
            <label htmlFor="password">Senha de acesso à demonstração</label>
            <input ref={passwordRef} id="password" type="password" name="password" autoComplete="current-password"
              required disabled={retail.busy} placeholder="Digite sua senha" aria-describedby="password-note" />
            <p id="password-note" className="muted">Acesso protegido por sessão segura. Sua senha não é armazenada no navegador.</p>
            {retail.error && <p className="error-message" role="alert">{retail.error}</p>}
            <button type="submit" className="button button-primary gate-submit" disabled={retail.busy}>
              {retail.busy ? <><span className="spinner" />Validando acesso…</> : <>Entrar na Vitrine <Icon name="arrow" /></>}
            </button>
          </form>}
        <div className="gate-promise"><span className="origin-mark origin-agent" /><p>Uma loja fictícia. Modelos reais.<br /><strong>Decisões sempre suas.</strong></p></div>
      </section>
    </main>
    <footer className="gate-footer"><span>VITRINE · LOJA CONCEITO</span><span>Sem dados de clientes reais. Sem reconhecimento facial.</span></footer>
  </div>;
}

function ConfirmDialog({ children, title, onClose, onConfirm, busy, disabled, confirmLabel }: {
  children: ReactNode; title: string; onClose: () => void; onConfirm: () => void;
  busy: boolean; disabled: boolean; confirmLabel: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  return <dialog ref={ref} className="confirm-dialog" aria-labelledby="confirm-title" onCancel={onClose} onClick={event => {
    if (event.target === ref.current) {
      const bounds = ref.current.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) onClose();
    }
  }}>
    <div className="dialog-heading"><span className="eyebrow">APROVAÇÃO HUMANA</span><button autoFocus className="button button-icon" aria-label="Fechar confirmação" onClick={onClose}>×</button></div>
    <h2 id="confirm-title">{title}</h2>{children}
    <div className="dialog-actions"><button className="button button-secondary" onClick={onClose}>Cancelar</button>
      <button className="button button-primary" disabled={disabled || busy} onClick={onConfirm}>{busy ? "Enviando…" : confirmLabel}</button></div>
  </dialog>;
}

function Experience() {
  const retail = useRetail();
  const { snapshot, connected, busy, runs } = retail;
  const [zone, setZone] = useState<ZoneId>("denim");
  const [tab, setTab] = useState<"live" | "experiment">("live");
  const [dialog, setDialog] = useState<{ kind: "reset"; scenario: string } | {
    kind: "approval"; run: AgentRun; recommendation: Recommendation; scenario: string;
  } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const insightsRef = useRef<HTMLDivElement>(null);
  const identity = snapshot ? `${snapshot.scenario.id}:${snapshot.scenario.activeSince}` : "";
  useEffect(() => { setDialog(null); setNotice(null); }, [identity, retail.auth]);
  if (retail.auth !== "in") return <Gate retail={retail} />;
  const pending = runs.some(run => run.status === "queued" || run.status === "running");
  const disabled = !connected || busy || !snapshot;
  const runAgent = (agent: AgentKind) => {
    if (disabled || pending) return;
    setNotice(null);
    void retail.act(signal => api.run(agent, agent === "merchandising"
      ? "Analise oportunidades de merchandising no cenário atual. Fundamente cada recomendação nas evidências disponíveis, cite os IDs e explicite limitações, distinguindo métricas simuladas de inferência real."
      : "Avalie o experimento atual comparando antes, depois e controle simulado. Explique a suficiência dos dados e as limitações sem atribuir causalidade real.", signal), retail.rememberRun);
    if (agent === "merchandising") insightsRef.current?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "start" });
  };
  const currentAction = dialog?.kind === "approval" && snapshot
    ? snapshot.availableActions.find(action => action.id === dialog.recommendation.actionId) : undefined;
  const currentRun = dialog?.kind === "approval" ? runs.find(run => run.id === dialog.run.id) : undefined;
  const approvalValid = !!(dialog?.kind === "approval" && identity === dialog.scenario && currentRun &&
    validRecommendations(currentRun, snapshot?.availableActions || []).some(item => item.recommendation.id === dialog.recommendation.id) &&
    currentAction && !currentAction.applied);
  const confirm = async () => {
    if (!dialog || disabled || dialog.scenario !== identity) return;
    if (dialog.kind === "reset") {
      if (await retail.act(signal => api.control("reset", undefined, signal), retail.acceptSnapshot)) {
        setDialog(null); setNotice("Cenário reiniciado. Um novo capítulo para a sua loja.");
      }
    } else if (approvalValid) {
      if (await retail.act(signal => api.intervene(dialog.run.id, dialog.recommendation.id, dialog.recommendation.actionId, signal))) {
        setDialog(null); setNotice("Intervenção aprovada e registrada. Aguardando a atualização do cenário.");
      }
    }
  };
  return <div className="app">
    <a className="skip-link" href="#main">Pular para o conteúdo</a>
    <header className="app-header">
      <Wordmark /><div className="header-divider" /><span className="store-label">Loja conceito<span>Moda · ambiente sintético</span></span>
      <div className="header-right"><WallClock /><ThemeButton /><button className="button button-ghost" onClick={() => void retail.logout()} disabled={busy}><Icon name="exit" /><span>Sair</span></button></div>
    </header>
    <main id="main" className="workspace">
      <div className="page-heading"><div><p className="eyebrow">OBSERVAR. COMPREENDER. EXPERIMENTAR.</p><h1>O pulso da sua <em>loja.</em></h1>
        <p className="page-description">Uma visão conectada da operação. Um espaço seguro para decidir.</p></div>
        <div className="connection-block"><span className={`connection ${connected ? "is-connected" : ""}`} role="status"><i />{connected ? "Conectado à loja" : snapshot ? "Conexão interrompida" : "Conectando à loja"}</span>
          <small>{retail.lastSync ? `Última atualização ${new Date(retail.lastSync).toLocaleTimeString("pt-BR")}` : "Aguardando a primeira resposta da API"}</small></div>
      </div>
      {retail.error && <div className="error-banner" role="alert"><span>{retail.error}</span><button className="button button-secondary" onClick={retail.retry} disabled={busy}>Tentar novamente</button><button className="button button-icon" aria-label="Dispensar erro" onClick={retail.dismissError}>×</button></div>}
      {notice && <div className="notice-banner" role="status"><span>{notice}</span><button className="button button-icon" aria-label="Fechar aviso" onClick={() => setNotice(null)}>×</button></div>}
      <div className="control-bar">
        <div className="scenario-label"><span className="badge badge-rose">SIMULADO</span><span>{snapshot ? snapshot.scenario.state === "running" ? "Cenário em andamento" : "Cenário pausado" : "Preparando cenário"}</span>
          <span className="simulation-clock">{snapshot ? formatClock(snapshot.scenario.elapsedSeconds) : "—"}</span></div>
        <div className="scenario-controls">
          <button className="button button-secondary" disabled={disabled} onClick={() => snapshot && void retail.act(signal => api.control(snapshot.scenario.state === "running" ? "pause" : "resume", undefined, signal), retail.acceptSnapshot)}>
            <Icon name={snapshot?.scenario.state === "running" ? "pause" : "play"} />{snapshot?.scenario.state === "running" ? "Pausar" : "Retomar"}</button>
          <div className="speed-control" role="group" aria-label="Velocidade da simulação">{([1, 2, 5] as const).map(speed => <button key={speed} className={snapshot?.scenario.speed === speed ? "selected" : ""} aria-label={`Velocidade ${speed} vezes`} aria-pressed={snapshot?.scenario.speed === speed}
            disabled={disabled} onClick={() => snapshot && void retail.act(signal => api.control(snapshot.scenario.state === "paused" ? "pause" : "resume", speed, signal), retail.acceptSnapshot)}>{speed}×</button>)}</div>
          <button className="button button-ghost" disabled={disabled} onClick={() => setDialog({ kind: "reset", scenario: identity })}><Icon name="reset" /><span>Reiniciar</span></button>
        </div>
      </div>
      <Provenance />
      {!snapshot ? <section className="warming-panel panel" role="status"><span className="warming-mark">V</span><p className="eyebrow">PREPARANDO SUA VISÃO DA LOJA</p><h2>Uma boa decisão começa<br />com dados de verdade.</h2><p>Aguardando o cenário autenticado. Câmeras, vendas e observações aparecerão conforme os serviços ficarem disponíveis.</p><span className="muted">Nenhum dado de demonstração é preenchido no navegador.</span></section>
        : <>
          {!connected && <div className="stale-banner" role="status">Exibindo a última resposta recebida. Vídeos e ações pausados neste dispositivo até a reconexão; o servidor pode continuar simulando.</div>}
          <section className="kpi-grid" aria-label="Indicadores do PDV simulado">
            {[
              { label: "Receita", value: formatMoney(snapshot.metrics.revenueCents, true), detail: "Acumulada no cenário", symbol: "↗" },
              { label: "Transações", value: snapshot.metrics.transactions.toLocaleString("pt-BR"), detail: "Movimentos do PDV", symbol: "≡" },
              { label: "Ticket médio", value: formatMoney(snapshot.metrics.averageBasketCents, true), detail: "Por transação simulada", symbol: "◈" },
              { label: "Visitas", value: snapshot.metrics.simulatedVisits.toLocaleString("pt-BR"), detail: "Do simulador, não das câmeras", symbol: "↳" },
            ].map((item, i) => <article className={`kpi-card ${i === 0 ? "kpi-featured" : ""}`} key={item.label}>
              <div className="kpi-top"><span>{item.label}</span><span className="kpi-symbol" aria-hidden="true">{item.symbol}</span></div>
              <strong>{item.value}</strong><div className="kpi-bottom"><span>{item.detail}</span><span className="kpi-source">SIMULADO</span></div>
            </article>)}
          </section>
          <div className="view-bar"><nav aria-label="Visões da loja" className="view-tabs">
            <button className={tab === "live" ? "active" : ""} aria-current={tab === "live" ? "page" : undefined} onClick={() => setTab("live")}>Operação ao vivo <span>01</span></button>
            <button className={tab === "experiment" ? "active" : ""} aria-current={tab === "experiment" ? "page" : undefined} onClick={() => setTab("experiment")}>Experimentos <span>02</span></button>
          </nav><button className="button button-primary analysis-cta" disabled={disabled || pending || snapshot.services.foundry !== "configured"} onClick={() => runAgent("merchandising")}>
            {pending ? <><span className="spinner" />Agente em execução</> : <>Analisar oportunidades <Icon name="arrow" /></>}</button></div>
          <div className="service-strip">
            <span><i className={snapshot.services.footageReady ? "service-ready" : "service-wait"} />Vídeos {snapshot.services.footageReady ? "disponíveis" : "em preparação"}</span>
            <span><i className={snapshot.services.vision === "ready" ? "service-ready" : "service-wait"} />Modelo {snapshot.services.vision === "ready" ? "disponível" : snapshot.services.vision === "warming" ? "aquecendo" : "indisponível"}</span>
            <span><i className={snapshot.services.foundry === "configured" ? "service-ready" : "service-wait"} />Foundry {snapshot.services.foundry === "configured" ? "configurado" : "indisponível"}</span>
            <span>Persistência {snapshot.services.persistence === "cosmos" ? "Cosmos DB" : "local"} · layout v{snapshot.scenario.layoutVersion}</span>
          </div>
          {snapshot.notices.length > 0 && <details className="system-notices"><summary>Avisos do cenário · {snapshot.notices.length}</summary><ul>{snapshot.notices.map((item, i) => <li key={i}>{item}</li>)}</ul></details>}
          {tab === "live" ? <>
            <div className="operation-grid"><CameraWall snapshot={connected ? snapshot : { ...snapshot, scenario: { ...snapshot.scenario, state: "paused" } }} selectedZone={zone} onSelectZone={setZone} />
              <FloorPlan snapshot={snapshot} selectedZone={zone} onSelectZone={setZone} /></div>
            <div className="commerce-grid"><SalesTrend series={snapshot.series} /><ReceiptFeed events={snapshot.posEvents} elapsedSeconds={snapshot.scenario.elapsedSeconds} scenarioId={identity} /></div>
          </> : <ExperimentPanel snapshot={snapshot} onEvaluate={() => runAgent("experiment")} busy={busy || pending} disabled={disabled || snapshot.services.foundry !== "configured"} />}
          <div ref={insightsRef} className="insights-anchor"><AgentPanel runs={runs} actions={snapshot.availableActions} onAnalyze={() => runAgent("merchandising")}
            onApprove={(run, recommendation) => setDialog({ kind: "approval", run, recommendation, scenario: identity })}
            busy={busy} disabled={disabled || snapshot.services.foundry !== "configured"} /></div>
        </>}
      <footer className="app-footer"><span>VITRINE <span className="muted">/ LOJA CONCEITO</span></span><span>Dados sintéticos. Observações verificáveis. Decisões humanas.</span><span>Uma experiência de varejo na nuvem</span></footer>
    </main>
    {dialog && <ConfirmDialog title={dialog.kind === "reset" ? "Começar um novo cenário?" : "Aplicar esta ideia à simulação?"} onClose={() => setDialog(null)}
      onConfirm={() => void confirm()} busy={busy} disabled={disabled || (dialog.kind === "approval" && !approvalValid)}
      confirmLabel={dialog.kind === "reset" ? "Reiniciar cenário" : "Confirmar e aplicar"}>
      {dialog.kind === "reset" ? <><p>O progresso, as vendas e as intervenções do cenário atual serão reiniciados. Os vídeos e as definições dos agentes serão preservados.</p><p className="dialog-note">Esta ação afeta apenas o ambiente sintético.</p></>
        : <><h3>{dialog.recommendation.title}</h3><p>{currentAction?.description || "Ação indisponível."}</p>
          <div className="dialog-preview"><span className="eyebrow">PRÉVIA DA MUDANÇA</span>
            <p>{dialog.recommendation.actionId === "reposition-denim" ? "Reorientar a disposição das araras de denim no cenário sintético, conforme a ação autorizada." : "Repor a disponibilidade de essenciais no estoque simulado, conforme a ação autorizada."}</p>
            <p>O servidor registra a intervenção e atualiza os parâmetros simulados. Isso não comprova aumento real de vendas.</p></div>
          {!!currentAction?.guardrails.length && <ul>{currentAction.guardrails.map((item, i) => <li key={i}>{item}</li>)}</ul>}
          {dialog.recommendation.caveats.length > 0 && <details><summary>Ressalvas da recomendação</summary><ul>{dialog.recommendation.caveats.map((item, i) => <li key={i}>{item}</li>)}</ul></details>}
          {!approvalValid && <p className="error-message" role="alert">Esta recomendação não está mais disponível para aplicação. Atualize a análise.</p>}
          <p className="dialog-note">Sua confirmação será vinculada à execução <code>{dialog.run.id}</code>. Nenhuma loja real será alterada.</p></>}
      {retail.error && <p className="error-message" role="alert">{retail.error}</p>}
    </ConfirmDialog>}
  </div>;
}

class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (this.state.failed) return <main className="warming-panel"><Wordmark /><h1>Não foi possível abrir esta visão.</h1><p>A resposta recebida não pôde ser exibida. Nenhum dado substituto foi utilizado.</p><button className="button button-primary" onClick={() => window.location.reload()}>Recarregar experiência</button></main>;
    return this.props.children;
  }
}

export default function App() { return <ErrorBoundary><Experience /></ErrorBoundary>; }

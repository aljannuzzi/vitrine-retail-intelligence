import { useEffect, useId, useState, useSyncExternalStore } from "react";
import type { AgentRun, AvailableAction, PosEvent, Recommendation, Snapshot, TimePoint } from "./types";
import { advanceReceiptHistory, buildTrend, formatClock, formatMoney, formatPercent, validRecommendations } from "./insights-utils";
import "./insights.css";

const integer = new Intl.NumberFormat("pt-BR");
const compact = new Intl.NumberFormat("pt-BR", { notation: "compact", maximumFractionDigits: 1 });
const timestamp = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "medium" });
const priorityLabels = { high: "Alta", medium: "Média", low: "Baixa" };
const agentLabels = { merchandising: "Merchandising", experiment: "Avaliação de experimento" };

function dateLabel(value: string | null): string {
  if (!value) return "Não informado";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Data indisponível" : timestamp.format(date);
}

function subscribeMotion(callback: () => void) {
  const media = window.matchMedia("(prefers-reduced-motion: reduce)");
  media.addEventListener("change", callback);
  return () => media.removeEventListener("change", callback);
}

function motionSnapshot() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function serverMotionSnapshot() { return true; }

export function SalesTrend({ series }: { series: TimePoint[] }) {
  const id = useId();
  const trend = buildTrend(series);
  const latest = trend?.points[trend.points.length - 1];
  return (
    <section className="panel ins-panel trend-panel" aria-labelledby={`${id}-heading`}>
      <header className="section-heading ins-heading">
        <div>
          <p className="eyebrow">O ritmo da loja</p>
          <h2 id={`${id}-heading`}>Receita ao longo do tempo</h2>
        </div>
        <span className="badge">Simulação</span>
      </header>
      <p className="muted ins-intro">Série retornada pelo backend. O controle também é simulado, não uma loja real.</p>
      {!trend || !latest ? (
        <div className="empty-state">Aguardando os primeiros pontos de receita. Nenhuma curva foi estimada.</div>
      ) : (
        <>
          <div className="trend-key">
            <div><span className="trend-swatch" aria-hidden="true" /><span>Loja simulada</span><strong>{formatMoney(latest.revenueCents, true)}</strong></div>
            <div><span className="trend-swatch trend-swatch-control" aria-hidden="true" /><span>Controle simulado</span><strong>{formatMoney(latest.controlRevenueCents, true)}</strong></div>
          </div>
          <figure className="trend-figure">
            <svg className="trend-chart" viewBox="0 0 720 230" role="img" aria-labelledby={`${id}-title ${id}-description`}>
              <title id={`${id}-title`}>Receita da loja e do controle simulados, em reais</title>
              <desc id={`${id}-description`}>
                {trend.points.length} pontos do backend, de {formatClock(trend.minTime)} até {formatClock(trend.maxTime)} no relógio simulado.
                {" "}Última receita da loja: {formatMoney(latest.revenueCents, true)}; controle: {formatMoney(latest.controlRevenueCents, true)}.
                {" "}Linha contínua: loja. Linha tracejada: controle. Valores completos na tabela abaixo.
              </desc>
              {trend.ticks.map((value) => (
                <g key={value}>
                  <line className="trend-grid" x1="82" x2="692" y1={trend.y(value)} y2={trend.y(value)} />
                  <text className="trend-axis" x="72" y={trend.y(value) + 4} textAnchor="end">
                    {compact.format(value / 100)}
                  </text>
                </g>
              ))}
              <text className="trend-axis" x="72" y="16" textAnchor="end">R$</text>
              <polyline className="trend-line trend-line-control" points={trend.controlPath} />
              <polyline className="trend-line" points={trend.revenuePath} />
              <circle className="trend-dot-control" cx={trend.x(latest.elapsedSeconds)} cy={trend.y(latest.controlRevenueCents)} r="5" />
              <circle className="trend-dot" cx={trend.x(latest.elapsedSeconds)} cy={trend.y(latest.revenueCents)} r="3.5" />
              <text className="trend-axis" x={trend.x(trend.minTime)} y="216" textAnchor={trend.minTime === trend.maxTime ? "middle" : "start"}>{formatClock(trend.minTime)}</text>
              {trend.minTime !== trend.maxTime && <text className="trend-axis" x="692" y="216" textAnchor="end">{formatClock(trend.maxTime)}</text>}
            </svg>
            <figcaption className="muted">Tempo de simulação · {integer.format(trend.points.length)} pontos observados</figcaption>
          </figure>
          <details className="ins-details">
            <summary>Consultar valores da série</summary>
            <div className="ins-table-scroll" tabIndex={0} role="region" aria-label="Tabela de receita da simulação">
              <table className="ins-table">
                <caption>Valores reais da resposta da API; métricas simuladas.</caption>
                <thead><tr><th scope="col">Relógio</th><th scope="col">Loja</th><th scope="col">Controle</th><th scope="col">Transações</th><th scope="col">Visitas simuladas</th></tr></thead>
                <tbody>{trend.points.map((point, index) => (
                  <tr key={`${point.at}-${point.elapsedSeconds}-${index}`}>
                    <th scope="row">{formatClock(point.elapsedSeconds)}</th>
                    <td>{formatMoney(point.revenueCents, true)}</td><td>{formatMoney(point.controlRevenueCents, true)}</td>
                    <td>{integer.format(point.transactions)}</td><td>{integer.format(point.visits)}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          </details>
        </>
      )}
    </section>
  );
}

export function ReceiptFeed({ events, elapsedSeconds, scenarioId }: { events: PosEvent[]; elapsedSeconds: number; scenarioId: string }) {
  const id = useId();
  const reducedMotion = useSyncExternalStore(subscribeMotion, motionSnapshot, serverMotionSnapshot);
  const [history, setHistory] = useState(() => advanceReceiptHistory(null, events, elapsedSeconds, scenarioId, reducedMotion));
  useEffect(() => {
    setHistory((previous) => advanceReceiptHistory(previous, events, elapsedSeconds, scenarioId, reducedMotion));
  }, [events, elapsedSeconds, scenarioId, reducedMotion]);
  const sameTimeline = history.scenarioId === scenarioId && elapsedSeconds >= history.elapsedSeconds;
  const visible = [...events].filter((event) => event.elapsedSeconds <= elapsedSeconds)
    .sort((a, b) => b.elapsedSeconds - a.elapsedSeconds).slice(0, 12);
  return (
    <section className="panel ins-panel receipt-panel" aria-labelledby={`${id}-heading`}>
      <header className="section-heading ins-heading">
        <div><p className="eyebrow">No caixa</p><h2 id={`${id}-heading`}>Últimos movimentos</h2></div>
        <span className="badge">PDV simulado</span>
      </header>
      <div className="receipt-clock"><span>Relógio da simulação</span><strong>{formatClock(elapsedSeconds)}</strong></div>
      {!visible.length ? <div className="empty-state">Nenhum movimento recebido neste cenário. As vendas aparecerão quando o backend as registrar.</div> : (
        <ol className="receipt-list" tabIndex={0} aria-label="Movimentos recentes do PDV simulado">
          {visible.map((event) => (
            <li key={event.id} data-event-id={event.id}
              className={`receipt-row${!reducedMotion && sameTimeline && history.fresh.has(event.id) ? " receipt-new" : ""}`}
              onAnimationEnd={() => setHistory((previous) => {
                if (!previous.fresh.has(event.id)) return previous;
                const fresh = new Set(previous.fresh);
                fresh.delete(event.id);
                return { ...previous, fresh };
              })}>
              <span className="receipt-symbol" aria-hidden="true">{event.type === "return" ? "↩" : "↗"}</span>
              <div className="receipt-item">
                <div className="receipt-item-heading"><h3>{event.item}</h3>{event.type === "return" && <span className="receipt-return">Devolução</span>}</div>
                <p>{integer.format(event.units)} {event.units === 1 ? "unidade" : "unidades"} <span aria-hidden="true">·</span> Tam. {event.size}</p>
                <span className="receipt-sku">SKU {event.sku}</span>
              </div>
              <div className="receipt-value">
                <strong>{formatMoney(event.amountCents, true)}</strong>
                <span title={`Registrado em ${dateLabel(event.at)} · relógio simulado ${formatClock(event.elapsedSeconds)}`}>
                  há {formatClock(elapsedSeconds - event.elapsedSeconds)} <span className="ins-sr-only">no tempo da simulação</span>
                </span>
              </div>
            </li>
          ))}
        </ol>
      )}
      <p className="muted ins-footnote">{visible.length ? `Exibindo ${visible.length} movimentos mais recentes. ` : ""}Valores e unidades vêm do PDV sintético, não das câmeras.</p>
    </section>
  );
}

export function ExperimentPanel({ snapshot, onEvaluate, busy, disabled }: { snapshot: Snapshot; onEvaluate: () => void; busy: boolean; disabled: boolean }) {
  const id = useId();
  const experiment = snapshot.experiment;
  const status = { "not-started": "Aguardando intervenção", collecting: "Coletando observações", "simulated-comparison": "Comparação simulada" }[experiment.status];
  const interventions = [...snapshot.interventions].sort((a, b) => a.elapsedSeconds - b.elapsedSeconds);
  return (
    <section className="panel ins-panel experiment-panel" aria-labelledby={`${id}-heading`}>
      <header className="section-heading ins-heading">
        <div><p className="eyebrow">Da hipótese à observação</p><h2 id={`${id}-heading`}>O que mudou na simulação?</h2></div>
        <span className="badge">{status}</span>
      </header>
      <p className="muted ins-intro">Antes, depois e controle — sem confundir uma comparação simulada com evidência causal.</p>
      <dl className="experiment-metrics">
        <div className="experiment-metric"><dt>Antes da intervenção</dt><dd className={experiment.beforeRevenuePer100Visits === null ? "experiment-insufficient" : ""}>{formatMoney(experiment.beforeRevenuePer100Visits, true)}</dd><dd className="experiment-context"><span>Receita por 100 visitas simuladas</span><small>{integer.format(experiment.beforeTransactions)} transações</small></dd></div>
        <div className="experiment-metric"><dt>Depois da intervenção</dt><dd className={experiment.afterRevenuePer100Visits === null ? "experiment-insufficient" : ""}>{formatMoney(experiment.afterRevenuePer100Visits, true)}</dd><dd className="experiment-context"><span>Receita por 100 visitas simuladas</span><small>{integer.format(experiment.afterTransactions)} transações</small></dd></div>
        <div className="experiment-metric"><dt>Variação do controle</dt><dd className={experiment.controlChangePercent === null ? "experiment-insufficient" : ""}>{formatPercent(experiment.controlChangePercent)}</dd><dd className="experiment-context"><span>Controle simulado, não uma loja real</span></dd></div>
        <div className="experiment-metric"><dt>Variação relativa</dt><dd className={experiment.relativeChangePercent === null ? "experiment-insufficient" : ""}>{formatPercent(experiment.relativeChangePercent)}</dd><dd className="experiment-context"><span>Comparação descritiva · não causal</span></dd></div>
      </dl>
      <div className="experiment-caveat">
        <strong>Leitura responsável</strong>
        <p>{experiment.caveat || "Ainda não há uma conclusão retornada pelo backend."}</p>
        <p>Não há evidência de ganho causal no mundo real. As intervenções alteram somente o cenário e os parâmetros simulados.</p>
      </div>
      <div className="experiment-timeline-heading"><h3>Intervenções aprovadas</h3><span className="muted">{integer.format(interventions.length)} registradas</span></div>
      {!interventions.length ? <div className="empty-state">Nenhuma intervenção aprovada. O histórico começa após uma decisão humana.</div> : (
        <ol className="experiment-timeline">
          {interventions.map((intervention) => (
            <li key={intervention.id}>
              <span className="experiment-time">{formatClock(intervention.elapsedSeconds)}</span>
              <div><h4>{intervention.title}</h4><p className="muted">Layout v{intervention.layoutVersion} · alteração na simulação</p>
                <details className="ins-details experiment-trace"><summary>Ver registro da intervenção</summary>
                  <dl className="ins-metadata"><div><dt>Registro</dt><dd>{intervention.id}</dd></div><div><dt>Execução de origem</dt><dd>{intervention.sourceRunId}</dd></div><div><dt>Data</dt><dd>{dateLabel(intervention.at)}</dd></div></dl>
                </details>
              </div>
            </li>
          ))}
        </ol>
      )}
      {experiment.interventionAt && <p className="muted ins-footnote">Referência da comparação: {dateLabel(experiment.interventionAt)}.</p>}
      <footer className="ins-panel-footer"><p className="muted">O agente avalia as observações disponíveis e explicita suas limitações.</p>
        <button className="button button-secondary" type="button" onClick={onEvaluate} disabled={disabled || busy}>{busy ? "Aguarde…" : "Avaliar resultado"}</button>
      </footer>
    </section>
  );
}

function RunInspector({ run }: { run: AgentRun }) {
  return (
    <details className="ins-details agent-inspector">
      <summary>Inspecionar execução</summary>
      <dl className="ins-metadata">
        <div><dt>Agente registrado</dt><dd>{run.agentName || "Não informado"}</dd></div>
        <div><dt>Versão</dt><dd>{run.agentVersion ?? "Não informada"}</dd></div>
        <div><dt>Execução</dt><dd>{run.id}</dd></div>
        <div><dt>responseId</dt><dd>{run.responseId ?? "Não informado"}</dd></div>
        <div><dt>conversationId</dt><dd>{run.conversationId ?? "Não informado"}</dd></div>
        <div><dt>Criada em</dt><dd>{dateLabel(run.createdAt)}</dd></div>
        <div><dt>Concluída em</dt><dd>{run.completedAt ? dateLabel(run.completedAt) : "Não concluída"}</dd></div>
        <div><dt>Execução declarada</dt><dd>{run.execution}</dd></div>
      </dl>
      <h4>Chamadas de ferramenta · {run.toolCalls.length}</h4>
      {run.toolCalls.length ? <ol className="agent-tool-list">{run.toolCalls.map((call, index) => <li key={`${index}-${call}`}><code>{call}</code></li>)}</ol> : <p className="muted">Nenhuma chamada de ferramenta retornada.</p>}
    </details>
  );
}

export function AgentPanel({ runs, actions, onAnalyze, onApprove, busy, disabled }: {
  runs: AgentRun[];
  actions: AvailableAction[];
  onAnalyze: () => void;
  onApprove: (run: AgentRun, recommendation: Recommendation) => void;
  busy: boolean;
  disabled: boolean;
}) {
  const id = useId();
  const sorted = [...runs].sort((a, b) => (Date.parse(b.createdAt) || 0) - (Date.parse(a.createdAt) || 0));
  const pending = sorted.filter((run) => run.status === "queued" || run.status === "running");
  const active = pending.find((run) => run.status === "running") ?? pending[0];
  const settled = sorted.filter((run) => run.status === "completed" || run.status === "failed");
  return (
    <section className="panel ins-panel agent-panel" aria-labelledby={`${id}-heading`}>
      <header className="section-heading ins-heading">
        <div><p className="eyebrow">Inteligência com responsabilidade</p><h2 id={`${id}-heading`}>Uma recomendação. Sua decisão.</h2></div>
        <span className="badge">Azure AI Foundry</span>
      </header>
      <p className="muted ins-intro">Agentes registrados analisam evidências. Nenhuma recomendação altera a loja sem aprovação humana.</p>
      <div className="agent-provenance" aria-label="Origem das informações">
        <p><strong>Execução real</strong><span>Agentes no Azure AI Foundry.</span></p>
        <p><strong>Métricas simuladas</strong><span>Vendas, visitas e controle sintéticos.</span></p>
        <p><strong>Observações por inferência</strong><span>Modelo de visão sobre vídeos sintéticos; não mede vendas.</span></p>
      </div>
      <div className="agent-toolbar">
        <p className="muted">A análise usa o cenário atual e pode levar alguns instantes.</p>
        <button className="button button-primary" type="button" onClick={onAnalyze} disabled={disabled || busy || pending.length > 0}>
          {active ? "Análise pendente" : busy ? "Aguarde…" : "Analisar oportunidades"}
        </button>
      </div>
      {active && (
        <div className="agent-pending">
          <p role="status"><span className="agent-status-dot" aria-hidden="true" /><strong>{active.status === "running" ? "Execução em andamento no Foundry" : "Solicitação na fila"}</strong>
            <span>O resultado ainda não está disponível.{pending.length > 1 ? ` ${pending.length} execuções pendentes.` : ""}</span>
          </p>
          <p className="muted">{active.agentName || agentLabels[active.agent]}{active.agentVersion ? ` · v${active.agentVersion}` : ""}</p>
          <RunInspector run={active} />
        </div>
      )}
      {!runs.length && <div className="empty-state">O próximo insight começa com uma pergunta ao cenário. Solicite uma análise para receber recomendações fundamentadas.</div>}
      <div className="agent-runs">
        {settled.map((run) => {
          const recommendations = validRecommendations(run, actions);
          return (
            <article className="agent-run" key={run.id} aria-labelledby={`${id}-${run.id}`}>
              <header className="agent-run-heading">
                <div><p className="eyebrow">{agentLabels[run.agent]}</p><h3 id={`${id}-${run.id}`}>{run.agentName || "Agente não informado"}</h3><span className="muted">{run.agentVersion ? `Versão ${run.agentVersion}` : "Versão não informada"}</span></div>
                <span className="badge">{run.status === "failed" ? "Falhou" : "Concluída"}</span>
              </header>
              {run.status === "failed" ? <p className="error-message">{run.error || "A execução falhou. Nenhuma recomendação desta execução está disponível para aprovação."}</p> : (
                <>
                  <p className="agent-summary">{run.summary || "A execução não retornou um resumo."}</p>
                  {!recommendations.length && <p className="muted">Nenhuma recomendação válida vinculada a uma ação disponível nesta execução.</p>}
                  {recommendations.map(({ recommendation, action }) => (
                    <section className="agent-recommendation" key={recommendation.id} aria-labelledby={`${id}-${run.id}-${recommendation.id}`}>
                      <div className="agent-recommendation-top"><span className="eyebrow">Recomendação</span><span className="agent-priority">Prioridade {priorityLabels[recommendation.priority].toLocaleLowerCase("pt-BR")}</span></div>
                      <h4 id={`${id}-${run.id}-${recommendation.id}`}>{recommendation.title}</h4>
                      <p className="agent-rationale">{recommendation.rationale}</p>
                      <div className="agent-evidence"><h5>Evidências citadas</h5><ul>{recommendation.evidenceIds.map((evidence, index) => <li key={`${evidence}-${index}`}><code>{evidence}</code></li>)}</ul></div>
                      <div className="agent-caveats"><h5>Ressalvas</h5>{recommendation.caveats.length ? <ul>{recommendation.caveats.map((caveat, index) => <li key={index}>{caveat}</li>)}</ul> : <p>Nenhuma ressalva adicional retornada pelo agente.</p>}</div>
                      <div className="agent-action"><h5>Ação disponível: {action.title}</h5><p>{action.description}</p>
                        {action.guardrails.length > 0 && <><h5>Limites da ação</h5><ul>{action.guardrails.map((guardrail, index) => <li key={index}>{guardrail}</li>)}</ul></>}
                      </div>
                      <div className="agent-approval"><span>{action.applied ? "Intervenção já aplicada à simulação." : "Requer sua confirmação. Afeta somente a simulação."}</span>
                        <button className="button button-secondary" type="button" disabled={disabled || busy || action.applied}
                          aria-label={action.applied ? `Ação já aplicada: ${action.title}` : `Revisar e aprovar: ${recommendation.title}`}
                          onClick={() => onApprove(run, recommendation)}>{action.applied ? "Já aplicada" : "Aplicar na simulação"}</button>
                      </div>
                    </section>
                  ))}
                </>
              )}
              <div className="agent-limitations"><h4>Limitações da análise</h4>{run.limitations.length ? <ul>{run.limitations.map((limitation, index) => <li key={index}>{limitation}</li>)}</ul> : <p className="muted">Nenhuma limitação adicional retornada. A análise não comprova impacto causal real.</p>}</div>
              <RunInspector run={run} />
            </article>
          );
        })}
      </div>
    </section>
  );
}
